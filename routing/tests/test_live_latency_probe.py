"""Pinned matrix, budget, and the 422-after-55s anomaly hypothesis for the
live-deployed-hardware latency probe (plan 18-11).

Every constant below is fixed in this module's own commit, before a single
live request has ever been issued by `probe_live_latency.py` (a later
commit). That command imports every one of these names rather than
declaring a copy of its own -- the same single-shared-source-of-truth
discipline `test_dispatch_predictor.py` (D-19-style) and
`test_solver_latency.py` (D-19/D-21) already established for this phase.

## Why this measurement exists

Every latency figure this phase has recorded before this plan came from a
workstation (1.556s), a CI runner (8.12s), or a single live capture on
Dallas -> LA (8.399s solver stage) -- never from the live instance's own
`Server-Timing` on the exact corridor ROADMAP criterion 1 uses as its own
worked example (Dallas -> Seattle at the API default vehicle). That gap is
exactly why a live HTTP 500 on that corridor survived nine plans and a full
suite gate (`18-VERIFICATION.md`). This module pins the protocol that
closes it: wake first (a ~42s cold boot must never be charged to the
solver), bust the cache so a 1-2ms warm hit is never mistaken for a fast
solve, keep the client timeout strictly above the deployed worker timeout
so a breach reads as a server 500 rather than an indistinguishable client
abort, and record a timeout as data, never as a discarded attempt.

## The probe matrix (`LIVE_PROBE_CELLS`)

Two cells, both at the API default vehicle (`mpg=10`, `tank_range_mi=500`,
`starting_fuel=1` -- `routing/serializers.py`'s `DEFAULT_MPG` /
`DEFAULT_TANK_RANGE_MI` / `DEFAULT_STARTING_FUEL`), chosen from
`routing/services/dp.py`'s own `DP_TRANSITION_BUDGET=50_000` hotfix
calibration comment's "Estimates at the API default vehicle" table -- not
re-derived here:

  - **dallas_tx-seattle_wa** (mandatory: ROADMAP criterion 1's own worked
    example) -- estimate 61,912, over the 50,000 budget -> currently
    dispatches to `penalty_aware_heuristic`.
  - **sacramento_ca-salt_lake_city_ut** -- estimate 120, the SMALLEST in
    that same table -> currently dispatches to `exact_dp`. Chosen over a
    cell nearer the 50,000 boundary (e.g. `houston_tx-chicago_il` at
    48,912, only ~2.2% below budget) specifically because this plan's own
    `LIVE_PROBE_CACHE_BUST_LADDER` perturbs `starting_fuel` by up to 3%,
    which changes each node's usable range by a few miles -- enough to
    risk flipping a near-boundary cell's dispatch arm mid-sweep. A cell an
    order of magnitude below the boundary cannot flip, so the exact_dp arm
    this plan measures is guaranteed to stay the exact_dp arm across every
    ladder rung. The tradeoff, stated plainly: this is a genuinely light
    corridor, so its deployed exact_dp solver time is expected to be small
    (sub-second) -- a real measurement, not a synthetic one, but not a
    stress test of the exact_dp path either.

Both cells already have committed Mapbox Directions geometry fixtures
(`routing/tests/fixtures/corridor_geometry/`), so `probe_live_latency.py`
can build the exact request bodies without a live geocode.

## Why the workstation baseline is measured fresh, not cited verbatim

`dp.py`'s own calibration comment gives committed wall-clock workstation
figures, but at a DIFFERENT vehicle (`starting_fuel=0.5`, the
`test_solver_dispatch.CALIBRATION_CELLS` vehicle) than the API-default
`starting_fuel=1` this probe's live cells use -- and neither committed
table gives a wall-clock figure for `sacramento_ca-salt_lake_city_ut` at
all. Citing a mismatched-vehicle figure as this plan's "workstation"
baseline would compare two different inputs and call it one ratio.
`probe_live_latency.py` therefore measures its own workstation baseline,
in-process, over the EXACT SAME (cell, vehicle) pairs the live sweep uses,
immediately before issuing any live request -- an apples-to-apples
comparison rather than a reuse of a differently-parameterized historical
number. The historical committed figures stay on the record above for
context, not as the ratio's numerator.

## The 422-after-55s anomaly

- **Observation.** Dallas -> Seattle at `{mpg: 10, tank_range_mi: 1050,
  starting_fuel: 0}` returned HTTP 422 after 55.0s, outliving the 30s
  worker timeout without becoming a 500 (recorded in this plan's own
  "Measured evidence" table, pre-hotfix build `8946567`).
- **Hypothesis.** The 55s is dominated by a Render free-tier cold
  container boot (`.github/workflows/keep-warm.yml`'s own measured
  ~41.95s), which gunicorn's per-request `--timeout` does not bound
  because it starts counting only once a worker accepts the request; and
  the 422 itself is CORRECT, not a fault -- an empty tank at the origin
  (`starting_fuel=0`) means START can reach nothing, so
  `dp.preflight_gap_check` reports infeasibility exactly as SOLV-05 and
  D-17 specify (infeasibility has exactly one source in this codebase,
  checked over the unpruned candidate list before any prune or DP/heuristic
  dispatch), cheaply and long before any real DP or heuristic search work
  happens.
- **Prediction.** Against a confirmed-warm service, the same request
  returns 422 in well under the worker timeout, and its `Server-Timing`
  carries no SUBSTANTIAL `solver` stage. (`routing/timing.py`'s
  `_Stage.__exit__` always records elapsed time, on success AND on
  exception, and `solver.solve()` raises `InfeasibleRouteError` from
  `preflight_gap_check` INSIDE `views.py`'s `with
  self._timing.stage("solver")` block -- so the `solver` stage name will
  still appear in the header. "No substantial solver stage" means a
  near-zero duration consistent with one cheap sort-plus-scan over the
  candidate list, not the multi-second span real DP or heuristic search
  work takes; see `ANOMALY_SOLVER_STAGE_MAX_MS` below for the pinned
  operational cutoff between the two.)
- **Falsification.** If the request still exceeds
  `ANOMALY_FALSIFICATION_THRESHOLD_SECONDS` against a confirmed-warm
  service, or returns 422 with a `solver` stage duration exceeding
  `ANOMALY_SOLVER_STAGE_MAX_MS`, the hypothesis is REFUTED and the anomaly
  is recorded as UNEXPLAINED with its measured figures. It is not to be
  re-explained with a second hypothesis invented after the numbers land,
  and no constant above is to be adjusted to make it fit.

- **Verdict: CONFIRMED.** Measured against a confirmed-warm service
  (`manage.py probe_live_latency --anomaly`, plan 18-11 task 3): the wake
  step reported status 200 in 1.13s (already warm from this same session's
  earlier sweep, task 2), then the exact anomaly request returned HTTP 422
  in 6.53s wall time -- comfortably under `ANOMALY_FALSIFICATION_
  THRESHOLD_SECONDS=15` -- with `Server-Timing` stages `eia;dur=0.9,
  cache;dur=0.9, route;dur=1631.4, index;dur=0.0, corridor;dur=3805.0,
  solver;dur=0.5, total;dur=5436.9`. The `solver` stage was 0.5ms, far
  under `ANOMALY_SOLVER_STAGE_MAX_MS=500` -- confirming
  `dp.preflight_gap_check` rejected the empty-tank request essentially
  instantly, exactly as the hypothesis predicted. The 6.53s wall time was
  dominated by `corridor` (3805.0ms) and `route` (1631.4ms) -- a genuine
  Mapbox Directions round trip plus corridor candidate filtering over the
  full unpruned list, neither of which this hypothesis was ever about.
  **The original 55s figure was cold-boot time, not solver time**:
  gunicorn's per-request `--timeout` counts only from when a worker
  accepts the request, so a container still booting when the request
  arrived would report its FULL wall-clock duration (boot plus request)
  with no way for gunicorn's own timeout to distinguish the two. This
  anomaly is CLOSED, not merely explained away: the hypothesis was written
  down before this measurement, the falsification thresholds were pinned
  before this measurement, and the measurement met both.
"""
import os
import re
from decimal import Decimal
from pathlib import Path

from django.test import SimpleTestCase

# --- LIVE_PROBE_BASE_URL ----------------------------------------------------
#
# The deployed origin this probe measures. Overridable via the
# LIVE_PROBE_BASE_URL environment variable (e.g. to point at a local
# container for a dry run), with the deployed URL as the default -- only
# the deployed origin produces a figure this phase may cite as
# "deployed-hardware" evidence; a local-container run is a smoke test of
# this module's own plumbing, never a substitute measurement.
LIVE_PROBE_BASE_URL = os.environ.get("LIVE_PROBE_BASE_URL", "https://tankwise.onrender.com")

# --- LIVE_PROBE_MAX_REQUESTS -------------------------------------------------
#
# Hard ceiling on the total number of HTTP requests ONE invocation of
# `probe_live_latency.py` may issue (counting the wake step), enforced by
# the command itself before it issues a single request -- a measurement
# that throttles the product it measures is a failed measurement.
#
# Derived from `render.yaml`'s deployed ceilings: `ROUTE_THROTTLE_SUSTAINED_
# RATE=200/day` is counted PER CLIENT IP (the AnonRateThrottle scope this
# probe's own machine would consume against), and `MAPBOX_DAILY_CALL_CAP
# =3000` is the deployment-wide outbound-Mapbox ceiling shared with every
# real caller. This probe's own worst-case implied request count (see
# `probe_live_latency.py`'s budget check, `len(LIVE_PROBE_CELLS) *
# LIVE_PROBE_REPEATS * len(LIVE_PROBE_CACHE_BUST_LADDER) + 1` = `2 * 2 * 3 +
# 1` = 13) sits comfortably under 20 -- 10% of the 200/day per-IP throttle,
# leaving 90% of that window for the live demo on the same IP and enough
# headroom for a full repeat run the same day (20 * 2 = 40, still 20% of
# 200) without ever approaching the throttle boundary this probe's own
# threat model (T-18-39) exists to respect.
LIVE_PROBE_MAX_REQUESTS = 20

# --- LIVE_PROBE_INTER_REQUEST_SECONDS ---------------------------------------
#
# Minimum spacing between consecutive requests. Derived from
# `render.yaml`'s `ROUTE_THROTTLE_BURST_RATE=20/min`: the mathematical
# minimum safe spacing is 60/20 = 3.0s; 4.0s adds a flat margin above that
# minimum so ordinary request/response jitter (DNS, TLS handshake, Render's
# own edge proxy) cannot accidentally push two requests into the same
# rolling 60s burst window.
LIVE_PROBE_INTER_REQUEST_SECONDS = 4.0

# --- LIVE_PROBE_WAKE_TIMEOUT_SECONDS -----------------------------------------
#
# Budget for the wake step (a GET to the dependency-free liveness probe,
# `/api/health`). Taken directly from `.github/workflows/keep-warm.yml`'s
# own measured evidence: a cold curl of that same probe during planning
# returned 200 after 41.95s, and that workflow already budgets 90s for it
# (`--max-time 90`). Reused verbatim rather than re-derived.
LIVE_PROBE_WAKE_TIMEOUT_SECONDS = 90

# --- LIVE_PROBE_REQUEST_TIMEOUT_SECONDS --------------------------------------
#
# Client-side timeout for every measured /api/route POST. STRICTLY GREATER
# than the deployed `GUNICORN_TIMEOUT=30` (`render.yaml`, `entrypoint.sh`):
# a client timeout AT OR UNDER the worker timeout converts a server-side
# breach into a client-side abort, which is indistinguishable from a
# network fault and would have hidden this phase's entire gap (the
# Dallas -> Seattle HTTP 500 that survived nine prior plans). This constant
# sitting strictly above 30 is what makes a breach OBSERVABLE rather than
# silently swallowed as "the connection timed out." 45s gives 15s of
# margin above the deployed timeout -- enough to see a breach clearly
# without tying up the probe indefinitely on a truly hung connection.
LIVE_PROBE_REQUEST_TIMEOUT_SECONDS = 45

# --- LIVE_PROBE_CELLS ---------------------------------------------------------
#
# See the module docstring's "The probe matrix" section for the full
# derivation and provenance of both cells and why sacramento was chosen
# over a cell nearer the DP_TRANSITION_BUDGET boundary. `vehicle` values are
# plain strings (not Decimal) so they serialize directly into a JSON
# request body without a custom encoder; `probe_live_latency.py` overrides
# `starting_fuel` per-request from `LIVE_PROBE_CACHE_BUST_LADDER`.
LIVE_PROBE_CELLS = (
    {
        "slug": "dallas_tx-seattle_wa",
        "label": "Dallas, TX -> Seattle, WA",
        "start": "32.7767,-96.7970",
        "finish": "47.6062,-122.3321",
        "vehicle": {"mpg": "10", "tank_range_mi": "500", "starting_fuel": "1"},
        "known_dispatch_arm": "penalty_aware_heuristic",
    },
    {
        "slug": "sacramento_ca-salt_lake_city_ut",
        "label": "Sacramento, CA -> Salt Lake City, UT",
        "start": "38.567694,-121.468161",
        "finish": "40.776928,-111.930991",
        "vehicle": {"mpg": "10", "tank_range_mi": "500", "starting_fuel": "1"},
        "known_dispatch_arm": "exact_dp",
    },
)

# --- LIVE_PROBE_CACHE_BUST_LADDER --------------------------------------------
#
# Pinned `starting_fuel` perturbation rungs used to force a distinct
# `route:v6:` cache key per repeat. `routing/cache.py`'s `_vehicle_token`
# quantizes `starting_fuel` to `FUEL_PRECISION=3` decimal places (NOT the
# 2 decimal places its own module docstring might suggest by analogy with
# `mpg` -- `MPG_PRECISION=2`, `TANK_PRECISION=1`, `FUEL_PRECISION=3` are
# three DIFFERENT precisions, one per vehicle field), so a perturbation at
# the hundredths place (this ladder's own granularity) survives that
# rounding and always produces a distinct key. Each distinct key costs
# exactly one outbound Mapbox Directions call on its first hit -- which is
# why this ladder is short (3 rungs: one per `LIVE_PROBE_REPEATS` slot,
# plus exactly one spare rung for the single permitted cache-hit retry
# `probe_live_latency.py` performs). All three rungs stay within 3% of the
# API-default `starting_fuel=1` (a full tank), nowhere near 0 -- far too
# small a perturbation to affect feasibility (a few miles of usable range
# on a 500-mile-tank corridor) or, for `sacramento_ca-salt_lake_city_ut`
# (estimate 120, two orders of magnitude below the 50,000 dispatch
# boundary), to have any realistic chance of flipping its dispatch arm.
LIVE_PROBE_CACHE_BUST_LADDER = (Decimal("0.99"), Decimal("0.98"), Decimal("0.97"))

# --- LIVE_PROBE_REPEATS -------------------------------------------------------
#
# Measured samples per cell, worst-of-N reported (this phase's established
# worst-not-mean convention, e.g. `measure_solver_latency.py`,
# `measure_dispatch_predictor.py`). Pinned at 2, not 3 -- justified against
# the request budget above, not against statistical taste: at 2 cells this
# still keeps the sweep's own implied request count (see
# `LIVE_PROBE_MAX_REQUESTS`'s derivation) small relative to the 200/day
# per-IP throttle shared with real reviewer traffic, while still producing
# a genuine "worst of repeats" figure rather than a single, possibly-noisy
# sample.
LIVE_PROBE_REPEATS = 2

# --- RECOVERY_PROBE_CELLS (D-16/D-17) ----------------------------------------
#
# The COMMITTED OUTPUT of `select_live_probe_cells()`
# (`routing.tests.test_dispatch_recovery`), applied to plan 18.1-07's
# measured 26-cell offline rows (`18.1-07-SUMMARY.md`'s verbatim per-cell
# table) at plan 18.1-08's ADOPTED rung -- 130,000, the genuine
# `adopt_budget_rung()` result plan 18.1-08 measured against real
# DP-attempt timings and recorded verbatim in `dp.py`'s own
# `DP_TRANSITION_BUDGET` comment, even though `DP_TRANSITION_BUDGET` itself
# was NOT raised to that value (stays 50,000 -- MEASURED, NOT SHIPPED; see
# `18.1-08-SUMMARY.md`). D-17's rule is applied to the rung the pinned
# `adopt_budget_rung()` genuinely selected, not to the currently-shipped
# constant -- the rung is what decides which cell counts as "newly
# admitted" for the third probe slot; the shipped constant is a separate,
# deliberately unresolved shipping decision.
#
# Reproduced by direct call, not by hand: `select_live_probe_cells(rows,
# 130_000)` returns, in order:
#
#   1. Both `_LIVE_PROBE_RETENTION_FLOOR_CELLS`, always, first:
#      `sacramento_ca-salt_lake_city_ut`@500mi, `dallas_tx-seattle_wa`
#      @1050mi.
#   2. A third cell chosen from whatever is newly admitted in the
#      (70,000, 130,000] band relative to the 50,000 baseline. The only
#      row in that band is `dallas_tx-seattle_wa`@1050mi itself (estimate
#      117,895) -- already present as a retention-floor member, so the
#      rule's own "already in the set" branch applies and it is not
#      selected twice. No OTHER row falls in that band (the next-lowest
#      newly-admitted estimate above 70,000 is 150,905, at rung 200,000 --
#      outside the (70,000, 130,000] window this rung opens). With no
#      distinct cell available in-band, the rule's own fallback applies:
#      the demo cell (`is_demo_cell=True`) with the largest
#      `offline_untimed_solve_seconds` among plan 07's rows --
#      `demo_la_ca-denver_co-chicago_il`@1050mi (0.0818s), ranked above
#      `demo_la_ca-new_york_ny`@1050mi (0.0021s).
#
# Stated plainly, per this plan's own instruction: `dallas_tx-seattle_wa`
# IS in this set. It is selected mechanically, by rule, as one of the two
# mandatory D-17 retention-floor cells -- not because it separately won
# the third-slot ranking (it did not; it was already present when that
# ranking's fallback ran). No cell was added or removed by hand after
# seeing this output.
#
# `demo_la_ca-denver_co-chicago_il` is a multi-leg demo chip (one
# waypoint, Denver CO) -- its `waypoints` tuple is threaded through
# unchanged by `probe_live_latency.py`'s `--recovery` sweep, the one
# extension `_post_route`/`_measure_one_repeat` needed to stay reusable
# for a cell shape the main probe matrix (below) never carries.
#
# `vehicle` mpg/tank_range_mi per entry matches the exact vehicle plan
# 07's `estimate`/`offline_untimed_solve_seconds` figures above were
# measured against for that cell (`ADMISSION_MANIFEST_VEHICLE`: mpg=10 for
# the two corridor cells; `DEMO_CHIP_VEHICLE`: mpg=6.5 for the demo cell) --
# `starting_fuel` in each entry is a placeholder, unconditionally
# overridden per-repeat by `LIVE_PROBE_CACHE_BUST_LADDER`, exactly as the
# main probe matrix's own entries already work.
#
# `pre_phase_shipped_arm` is `recovery_verdict()`'s own
# `shipped_policy_strategy` input -- each cell's `ADMISSION_MANIFEST`
# boolean (`routing/tests/test_solver_dispatch.py`) translated to a
# `SolverStrategy` string: `sacramento_ca-salt_lake_city_ut`@500mi and
# `demo_la_ca-denver_co-chicago_il`@1050mi are both already `True`
# (`exact_dp`) under the unchanged 50,000 boundary; `dallas_tx-seattle_wa`
# @1050mi is `False` (`penalty_aware_heuristic`) -- the one cell this
# probe exists to check for recovery.
RECOVERY_PROBE_CELLS = (
    {
        "slug": "sacramento_ca-salt_lake_city_ut",
        "label": "Sacramento, CA -> Salt Lake City, UT",
        "start": "38.567694,-121.468161",
        "finish": "40.776928,-111.930991",
        "tank_range_mi": Decimal(500),
        "vehicle": {"mpg": "10", "tank_range_mi": "500", "starting_fuel": "0.5"},
        "known_dispatch_arm": "exact_dp",
        "pre_phase_shipped_arm": "exact_dp",
    },
    {
        "slug": "dallas_tx-seattle_wa",
        "label": "Dallas, TX -> Seattle, WA",
        "start": "32.7767,-96.7970",
        "finish": "47.6062,-122.3321",
        "tank_range_mi": Decimal(1050),
        "vehicle": {"mpg": "10", "tank_range_mi": "1050", "starting_fuel": "0.5"},
        "known_dispatch_arm": "penalty_aware_heuristic",
        "pre_phase_shipped_arm": "penalty_aware_heuristic",
    },
    {
        "slug": "demo_la_ca-denver_co-chicago_il",
        "label": "Los Angeles, CA -> Denver, CO -> Chicago, IL",
        "start": "34.0522,-118.2437",
        "waypoints": ("39.7392,-104.9903",),
        "finish": "41.8781,-87.6298",
        "tank_range_mi": Decimal(1050),
        "vehicle": {"mpg": "6.5", "tank_range_mi": "1050", "starting_fuel": "1"},
        "known_dispatch_arm": "exact_dp",
        "pre_phase_shipped_arm": "exact_dp",
    },
)


# --- The 422-after-55s anomaly reproduction ----------------------------------
#
# The exact request `18-VERIFICATION.md`'s own hotfix record and this
# plan's "Measured evidence" table cite: Dallas -> Seattle with an EXPLICIT
# vehicle profile (not the API default), `starting_fuel=0` -- an empty tank
# at the origin, which `dp.preflight_gap_check` must reject immediately.
ANOMALY_REQUEST = {
    "start": "32.7767,-96.7970",
    "finish": "47.6062,-122.3321",
    "vehicle": {"mpg": "10", "tank_range_mi": "1050", "starting_fuel": "0"},
}

# --- ANOMALY_FALSIFICATION_THRESHOLD_SECONDS ---------------------------------
#
# The pinned wall-clock cutoff deciding CONFIRMED vs REFUTED. Half of the
# deployed `GUNICORN_TIMEOUT=30` -- comfortably "well under the worker
# timeout" (the hypothesis's own wording) while leaving generous margin
# over the round-trip time an ordinary warm `/api/route` request actually
# takes (every warm figure this phase has recorded, cache hit or genuine
# solve, is well under 10s). A confirmed-warm request landing above this
# threshold cannot be explained by "a cheap pre-flight rejection" alone.
ANOMALY_FALSIFICATION_THRESHOLD_SECONDS = 15

# --- ANOMALY_SOLVER_STAGE_MAX_MS ---------------------------------------------
#
# The pinned cutoff (milliseconds) for what counts as a "substantial"
# `solver` Server-Timing stage in the falsification branch. Real DP or
# heuristic search work measured anywhere in this phase takes seconds, not
# milliseconds (the fastest committed workstation DP figure is still
# 1ms -- houston_tx-chicago_il @1050mi -- but that is a raw `solve_
# fixed_charge` call over a search set that already passed preflight; a
# preflight-only rejection does strictly less work than that: one sort
# plus one linear scan, no DP transitions at all). 500ms sits two-plus
# orders of magnitude below genuine search work while giving generous
# margin for Render free-tier shared-CPU jitter on a simple sort-and-scan.
ANOMALY_SOLVER_STAGE_MAX_MS = 500


class LiveLatencyProbeGuardTests(SimpleTestCase):
    """Runs entirely offline, no network access -- safe and fast inside the
    ordinary suite and inside CI. Exercises the header parser, the
    cache-hit rejection rule, and the censoring path against synthetic
    fixtures constructed in this test, never against a live response.

    Every test method below imports `ProbeRow`/`parse_server_timing` from
    `routing.management.commands.probe_live_latency` LOCALLY (inside the
    method, not at this module's top level) -- that command module itself
    imports every pinned constant from THIS module at ITS top level, so a
    top-level import back into it here would be circular: whichever
    module Python starts loading first would find the other only
    partially initialized (its own constants/names not yet bound) when
    the process reaches the reverse import. Deferring the import to
    inside each test method (the same pattern `measure_dispatch_
    predictor.py`'s own docstring already establishes for a different
    circularity reason) sidesteps this entirely, since by the time any
    test method actually runs, both modules are already fully loaded.

    Four assertions:

    1. The parser recovers each named stage from the exact multi-stage
       format `routing/timing.py` emits.
    2. A response carrying a `cache` stage and no `solver` stage is
       classified as a cache hit contributing no latency figure -- proven
       non-vacuous against a synthetic fixture, not merely asserted in the
       abstract.
    3. A synthetic 500 response produces a censored row that is PRESENT in
       the resulting table -- proven non-vacuous by actually filtering a
       row list and checking the censored row survives the filter, rather
       than only checking the `censored` flag in isolation.
    4. `LIVE_PROBE_REQUEST_TIMEOUT_SECONDS` exceeds the deployed
       `GUNICORN_TIMEOUT`, read from `render.yaml` AT TEST TIME rather than
       restated as a literal -- the anti-vacuity guard on the whole
       protocol. If this inequality ever inverts, every future breach
       becomes an invisible client abort and this probe stops being able
       to observe the failure it exists to catch (T-18-42).
    """

    def test_parser_recovers_each_named_stage_from_a_multi_stage_header(self):
        from routing.management.commands.probe_live_latency import parse_server_timing

        header = (
            "eia;dur=0.9, cache;dur=0.9, route;dur=1631.4, index;dur=0.0, "
            "corridor;dur=3805.0, solver;dur=0.5, total;dur=5436.9"
        )
        stages = parse_server_timing(header)
        self.assertEqual(
            stages,
            {
                "eia": 0.9,
                "cache": 0.9,
                "route": 1631.4,
                "index": 0.0,
                "corridor": 3805.0,
                "solver": 0.5,
                "total": 5436.9,
            },
        )

    def test_parser_returns_empty_dict_for_a_missing_or_empty_header(self):
        from routing.management.commands.probe_live_latency import parse_server_timing

        self.assertEqual(parse_server_timing(""), {})
        self.assertEqual(parse_server_timing(None), {})

    def test_cache_stage_without_solver_stage_is_classified_as_a_cache_hit(self):
        """Non-vacuous: builds the exact classification expression
        `probe_live_latency.py`'s own `_measure_one_repeat` uses, over a
        parsed synthetic header, and proves it evaluates True -- not just
        that the header CONTAINS `cache` and lacks `solver`."""
        from routing.management.commands.probe_live_latency import parse_server_timing

        header = "eia;dur=0.9, cache;dur=0.9"
        stages = parse_server_timing(header)
        self.assertIn("cache", stages)
        self.assertNotIn("solver", stages)

        cache_hit = ("solver" not in stages) and ("cache" in stages)
        self.assertTrue(
            cache_hit,
            "a response carrying a cache stage and no solver stage must "
            "be classified as a cache hit contributing no latency figure "
            "-- never as an impossibly fast solve.",
        )

    def test_a_censored_500_row_is_present_in_the_reported_table(self):
        """Non-vacuous: constructs a synthetic censored row exactly as
        `probe_live_latency.py`'s own classification would (status 500 ->
        `censored=True`), places it in a row list alongside a genuine-miss
        row, and proves a table-filtering step (the same
        `[r for r in rows if r.censored]` shape `_print_table`/`_print_
        factor_summary` group rows by) still surfaces it -- the exact
        selection bias that let a reproducible live 500 go unrecorded for
        nine prior plans."""
        from routing.management.commands.probe_live_latency import ProbeRow

        censored_row = ProbeRow(
            cell_label="synthetic_censored_cell",
            repeat_index=0,
            ladder_value=Decimal("0.99"),
            status_code=500,
            wall_time_s=30.5,
            stages_ms={},
            censored=True,
        )
        genuine_row = ProbeRow(
            cell_label="synthetic_censored_cell",
            repeat_index=1,
            ladder_value=Decimal("0.98"),
            status_code=200,
            wall_time_s=2.1,
            stages_ms={"solver": 87.0},
            censored=False,
        )
        rows = [censored_row, genuine_row]

        censored_rows_in_table = [r for r in rows if r.censored]
        self.assertEqual(
            len(censored_rows_in_table),
            1,
            "a censored row must remain present when the row list is "
            "filtered/grouped for reporting, never silently dropped.",
        )
        self.assertEqual(censored_rows_in_table[0].status_code, 500)
        self.assertEqual(censored_rows_in_table[0].wall_time_s, 30.5)
        self.assertFalse(censored_row.is_genuine_miss)

    def test_request_timeout_exceeds_the_deployed_gunicorn_timeout_from_render_yaml(self):
        """Anti-vacuity guard (T-18-42): reads GUNICORN_TIMEOUT out of
        render.yaml itself at test time, rather than restating "30" as a
        literal here -- so editing the deployment config alone (without
        touching this test) cannot silently break the inequality that
        makes a worker-timeout breach observable at all."""
        render_yaml_path = Path(__file__).resolve().parent.parent.parent / "render.yaml"
        content = render_yaml_path.read_text(encoding="utf-8")
        match = re.search(r"key:\s*GUNICORN_TIMEOUT\s*\n\s*value:\s*\"(\d+)\"", content)
        self.assertIsNotNone(
            match, "could not find a GUNICORN_TIMEOUT env var entry in render.yaml"
        )
        deployed_timeout_seconds = int(match.group(1))

        self.assertGreater(
            LIVE_PROBE_REQUEST_TIMEOUT_SECONDS,
            deployed_timeout_seconds,
            "LIVE_PROBE_REQUEST_TIMEOUT_SECONDS must stay strictly greater "
            "than the deployed GUNICORN_TIMEOUT -- if this ever inverts, "
            "a worker-timeout breach reads as an indistinguishable client "
            "abort instead of an observable server error.",
        )

    def test_recovery_probe_cells_stay_within_the_pinned_cell_and_request_budget(self):
        """D-17: `RECOVERY_PROBE_CELLS` must never grow past
        `LIVE_PROBE_MAX_CELLS`, and its own implied worst-case request
        count (entries * repeats * ladder rungs + one wake) must stay at
        or below `LIVE_PROBE_MAX_REQUESTS` -- the same budget arithmetic
        `test_dispatch_recovery.py`'s own well-formedness guard already
        checks for the rule in the abstract, re-checked here against the
        actual committed cell set."""
        from routing.tests.test_dispatch_recovery import LIVE_PROBE_MAX_CELLS

        self.assertLessEqual(len(RECOVERY_PROBE_CELLS), LIVE_PROBE_MAX_CELLS)

        implied_requests = (
            len(RECOVERY_PROBE_CELLS) * LIVE_PROBE_REPEATS * len(LIVE_PROBE_CACHE_BUST_LADDER)
            + 1
        )
        self.assertLessEqual(
            implied_requests,
            LIVE_PROBE_MAX_REQUESTS,
            "RECOVERY_PROBE_CELLS' own implied request count must stay "
            "inside the existing live-probe request budget.",
        )

    def test_every_recovery_probe_cell_carries_a_known_solver_strategy_value(self):
        """Every `known_dispatch_arm` and `pre_phase_shipped_arm` on
        `RECOVERY_PROBE_CELLS` must be one of the two real
        `SolverStrategy` values `solve()` actually emits -- catches a
        typo'd or stale strategy string that would silently never match
        anything `recovery_verdict()` compares against."""
        from routing.services.solver import SolverStrategy

        known_values = {SolverStrategy.EXACT_DP, SolverStrategy.PENALTY_AWARE_HEURISTIC}
        for cell in RECOVERY_PROBE_CELLS:
            self.assertIn(cell["known_dispatch_arm"], known_values)
            self.assertIn(cell["pre_phase_shipped_arm"], known_values)

    def test_recovery_probe_cells_carry_the_shape_live_probe_cells_entries_carry(self):
        """Every `RECOVERY_PROBE_CELLS` entry carries the same base shape
        the main probe matrix's entries already use (slug, label, start,
        finish, vehicle, known_dispatch_arm), plus the two D-17/D-18
        additions (tank_range_mi, pre_phase_shipped_arm) this plan's own
        verdict rule needs."""
        base_shape = {"slug", "label", "start", "finish", "vehicle", "known_dispatch_arm"}
        for cell in RECOVERY_PROBE_CELLS:
            self.assertTrue(base_shape.issubset(cell.keys()))
            self.assertIn("tank_range_mi", cell)
            self.assertIn("pre_phase_shipped_arm", cell)
