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

from routing.cache import FUEL_PRECISION
from routing.probe_seam import PROBE_BUDGET_HEADER, PROBE_KEY_HEADER
from routing.services import dp
from routing.services.prune import prune_dominated_candidates
from routing.services.solver import SolverStrategy
from routing.tests.test_corridor_fixtures import (
    CORRIDORS,
    DEMO_CHIP_VEHICLE,
    DEMO_CHIPS,
    TANK_RANGES_MI,
)
from routing.tests.test_solver_dispatch import (
    ADMISSION_MANIFEST,
    ADMISSION_MANIFEST_VEHICLE,
    STARTING_FUEL,
    RealCorridorDispatchTestCase,
)

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


# --- VERDICT_PROBE_CELLS (Phase 26 D-11, 2026-08-18) -------------------------
#
# The full 26-cell deployed-hardware verdict matrix D-11 needs to express
# 26 cells x 3 repeats, worst-of-3, plus K=5 on any band-admitted cell.
# DERIVED, not hand-written: the cross product of CORRIDORS (twelve slugs)
# x TANK_RANGES_MI (two pinned tank ranges) at ADMISSION_MANIFEST_VEHICLE's
# vehicle, plus both DEMO_CHIPS at DEMO_CHIP_VEHICLE's vehicle -- exactly
# ADMISSION_MANIFEST's own 26-cell key set
# (routing.tests.test_solver_dispatch), asserted identical by
# VerdictProbeMatrixGuardTests below, never re-typed by hand. This set is
# never hand-adjusted after a measurement -- the same pinned-before-
# measurement discipline every other threshold in this codebase follows.
#
# `offline_admitted` is read directly from ADMISSION_MANIFEST, never
# recomputed. `pre_phase_shipped_arm` is that boolean translated to the
# same SolverStrategy literal strings RECOVERY_PROBE_CELLS already
# carries.
#
# `base_starting_fuel` is each cell's OWN pinned starting fuel -- STARTING_
# FUEL (0.5) for the twelve corridor slugs, DEMO_CHIP_VEHICLE["starting_
# fuel"] (1) for the two demo slugs -- the concrete fix for RESEARCH.md's
# Pitfall 2: LIVE_PROBE_CACHE_BUST_LADDER perturbs starting_fuel around
# ~1.0, a DIFFERENT vehicle than the 0.5 ADMISSION_MANIFEST's twelve
# corridor cells were actually computed against. Plan 26-04's per-cell-
# vehicle-relative cache-bust (relative_cache_bust_starting_fuel, below)
# perturbs around THIS value instead, never a hardcoded 1.0. The
# `vehicle` dict's own `starting_fuel` string equals it exactly.
def build_verdict_probe_cells():
    """Pure derivation of the 26-cell verdict matrix -- see the module
    comment immediately above. Returns a tuple of per-cell dicts, never a
    literal tuple of 26 hand-written dicts."""
    cells = []

    for corridor in CORRIDORS:
        for tank_range_mi in TANK_RANGES_MI:
            key = (corridor.slug, int(tank_range_mi))
            cells.append(
                {
                    "slug": corridor.slug,
                    "label": corridor.label,
                    "start": f"{corridor.start[0]},{corridor.start[1]}",
                    "finish": f"{corridor.finish[0]},{corridor.finish[1]}",
                    "waypoints": (),
                    "tank_range_mi": tank_range_mi,
                    "vehicle": {
                        "mpg": str(ADMISSION_MANIFEST_VEHICLE["mpg"]),
                        "tank_range_mi": str(tank_range_mi),
                        "starting_fuel": str(STARTING_FUEL),
                    },
                    "base_starting_fuel": STARTING_FUEL,
                    "pre_phase_shipped_arm": (
                        SolverStrategy.EXACT_DP
                        if ADMISSION_MANIFEST[key]
                        else SolverStrategy.PENALTY_AWARE_HEURISTIC
                    ),
                    "offline_admitted": ADMISSION_MANIFEST[key],
                }
            )

    for chip in DEMO_CHIPS:
        key = (chip.slug, int(DEMO_CHIP_VEHICLE["tank_range_mi"]))
        waypoints = tuple(f"{lat},{lng}" for lat, lng in chip.waypoints)
        cells.append(
            {
                "slug": chip.slug,
                "label": chip.label,
                "start": f"{chip.start[0]},{chip.start[1]}",
                "finish": f"{chip.finish[0]},{chip.finish[1]}",
                "waypoints": waypoints,
                "tank_range_mi": DEMO_CHIP_VEHICLE["tank_range_mi"],
                "vehicle": {
                    "mpg": str(DEMO_CHIP_VEHICLE["mpg"]),
                    "tank_range_mi": str(DEMO_CHIP_VEHICLE["tank_range_mi"]),
                    "starting_fuel": str(DEMO_CHIP_VEHICLE["starting_fuel"]),
                },
                "base_starting_fuel": DEMO_CHIP_VEHICLE["starting_fuel"],
                "pre_phase_shipped_arm": (
                    SolverStrategy.EXACT_DP
                    if ADMISSION_MANIFEST[key]
                    else SolverStrategy.PENALTY_AWARE_HEURISTIC
                ),
                "offline_admitted": ADMISSION_MANIFEST[key],
            }
        )
    return tuple(cells)


VERDICT_PROBE_CELLS = build_verdict_probe_cells()

# --- VERDICT_PROBE_REPEATS (Phase 26 D-11, 2026-08-18) -----------------------
#
# Worst-of-3 matches recovery_verdict()'s own field shape
# (worst_of_repeats_total_response_seconds) and survives free-tier CPU
# sharing. DEMOTION_GUARD_PROBE_REPEATS=5 (test_dispatch_recovery.py) is
# deliberately NOT reused here -- it is reserved for the demotion-guard
# rewrite bar it was pinned as; using it everywhere would inflate this
# sweep's request cost without strengthening the pinned claim.
VERDICT_PROBE_REPEATS = 3


# --- relative_cache_bust_starting_fuel / VERDICT_PROBE_CACHE_BUST_FRACTIONS /
# PROBE_SEAM_CACHE_BUST_FRACTIONS (Phase 26 D-11, 2026-08-18) ----------------
def relative_cache_bust_starting_fuel(base_starting_fuel, fraction):
    """Perturb `base_starting_fuel` DOWNWARD by `fraction` (both `Decimal`),
    quantized to the SAME decimal precision `routing/cache.py`'s
    `_vehicle_token` quantizes `starting_fuel` to (`FUEL_PRECISION`,
    imported rather than restated as a literal) -- so the perturbed value
    always produces a distinct cache key, the same guarantee
    `LIVE_PROBE_CACHE_BUST_LADDER`'s absolute values already give.

    Downward only, and small: RESEARCH.md's Pitfall 2 is that
    `LIVE_PROBE_CACHE_BUST_LADDER` perturbs `starting_fuel` around ~1.0,
    while `ADMISSION_MANIFEST`'s twelve corridor cells are pinned at
    `STARTING_FUEL=0.5` -- probing that ladder verbatim on those cells
    measures a different vehicle than the manifest was computed against.
    This function perturbs RELATIVE TO EACH CELL'S OWN base fuel instead.

    Reducing starting fuel reduces the DP's reachable-state count from
    START and therefore the transition-count estimate `solve()` computes
    before dispatching -- it can only push a cell TOWARD the heuristic
    side of the `DP_TRANSITION_BUDGET` boundary, never toward the exact-DP
    side, so a near-boundary already-admitted cell (e.g.
    `houston_tx-chicago_il`@500mi at 48,926 of 50,000, 97.9%) cannot be
    pushed ACROSS the boundary by this perturbation alone. An UPWARD
    perturbation could, which would confound D-13's live-versus-offline
    comparison with a self-inflicted methodological cause. The magnitude
    is small either way: at most a 4% relative reduction (this module's
    lowest verdict rung, 0.96), a few miles of usable range on a
    500-mile tank -- far too small to affect feasibility on its own.

    Residual honesty note (D-13): the live column this ladder measures is
    taken at a starting fuel within 4% of ADMISSION_MANIFEST's own pinned
    value, not exactly at it. That deviation is recorded here rather than
    assumed immaterial.

    Never returns zero or a negative value -- both ladders below stay
    strictly inside (0, 1), so the product of a positive base and a
    strictly-positive fraction under 1 is always strictly positive; this
    is asserted defensively rather than trusted silently, because a
    zero/negative starting fuel would make every DP state at START
    unreachable (`InfeasibleRouteError`), not merely produce a distinct
    cache key.
    """
    value = Decimal(base_starting_fuel) * Decimal(fraction)
    quantum = Decimal(1).scaleb(-FUEL_PRECISION)
    quantized = value.quantize(quantum)
    if quantized <= 0:
        raise ValueError(
            f"relative_cache_bust_starting_fuel produced a non-positive "
            f"value ({quantized}) from base={base_starting_fuel!r} "
            f"fraction={fraction!r} -- refusing to return it."
        )
    return quantized


# Four rungs: VERDICT_PROBE_REPEATS(3) plus exactly one spare rung for the
# single permitted cache-hit retry `_measure_one_repeat` performs.
VERDICT_PROBE_CACHE_BUST_FRACTIONS = (
    Decimal("0.99"),
    Decimal("0.98"),
    Decimal("0.97"),
    Decimal("0.96"),
)

# Six rungs, STRICTLY DISJOINT from the ladder above -- covers Round 2's
# worst case (DEMOTION_GUARD_PROBE_REPEATS=5 plus one spare). Disjointness
# is load-bearing, not cosmetic: Round 2 re-probes stop chains Round 1
# already warmed on the same cache prefix, so a colliding rung would
# silently degrade a Round 2 repeat into a cache hit -- an
# indistinguishable-from-fast false reading, exactly the failure class
# this module's whole cache-busting protocol exists to prevent.
PROBE_SEAM_CACHE_BUST_FRACTIONS = (
    Decimal("0.95"),
    Decimal("0.94"),
    Decimal("0.93"),
    Decimal("0.92"),
    Decimal("0.91"),
    Decimal("0.90"),
)


# --- BAND_LADDER_RUNG_CELLS (Phase 26 D-05/D-06/D-11, plan 26-06) -----------
#
# For each real (non-`None`) `DP_TRANSITION_BUDGET_LADDER`
# (`test_dispatch_recovery.py`) rung ABOVE the shipped 50,000 baseline, the
# HEAVIEST cell that rung newly admits -- read off `25-MEASUREMENTS.md`
# section 2's CURRENT before-world estimates (which
# `ADMISSION_MANIFEST`'s own inline comments below now also carry, having
# been updated to the post-Phase-22-gap-fill figures), **NEVER** the STALE
# inline comments a re-pin can leave behind elsewhere in that file -- e.g.
# `san_diego_ca-jacksonville_fl`@500mi's own comment reads 66,571 there but
# measures 600,415 today; that class of drift is exactly why this mapping
# cites its source explicitly rather than being re-derived from a comment
# string at import time:
#
#   70,000  admits exactly one cell: dallas_tx-seattle_wa@500 (61,944)
#   130,000 additionally admits dallas_tx-seattle_wa@1050 (117,895)
#   200,000 adds atlanta_ga-denver_co@500 (150,905) AND
#           miami_fl-boston_ma@500 (182,506) -- the HEAVIER of the two,
#           miami_fl-boston_ma@500, is the one probed at this rung
#   400,000 adds jacksonville_fl-bangor_me@500 (356,085)
#
# Nothing else on the 26-cell grid is reachable by any real rung (both demo
# chips sit at tens of millions; see 26-CONTEXT.md's own `<specifics>`
# block for the full accounting).
#
# Each mapped value is the EXACT `VERDICT_PROBE_CELLS` entry for that
# `(slug, tank_range_mi)` pair -- never a hand-duplicated dict -- so the
# ladder sweep probes with the identical start/finish/vehicle/
# base_starting_fuel the 26-cell verdict matrix already pins.
def _verdict_cell(slug, tank_range_mi):
    for cell in VERDICT_PROBE_CELLS:
        if cell["slug"] == slug and int(cell["tank_range_mi"]) == tank_range_mi:
            return cell
    raise KeyError(
        f"no VERDICT_PROBE_CELLS entry for slug={slug!r} "
        f"tank_range_mi={tank_range_mi!r}"
    )


BAND_LADDER_RUNG_CELLS = {
    70_000: _verdict_cell("dallas_tx-seattle_wa", 500),
    130_000: _verdict_cell("dallas_tx-seattle_wa", 1050),
    200_000: _verdict_cell("miami_fl-boston_ma", 500),
    400_000: _verdict_cell("jacksonville_fl-bangor_me", 500),
}


# --- DEMOTION_BAR_PROBE_CELL (Phase 26 D-05/D-16, plan 26-06) --------------
#
# dallas_tx-seattle_wa@500mi at the EXACT API-default vehicle (`mpg=10`,
# `tank_range_mi=500`, `starting_fuel=1` -- `routing/serializers.py`'s
# `DEFAULT_MPG`/`DEFAULT_TANK_RANGE_MI`/`DEFAULT_STARTING_FUEL`) -- the
# precise request `routing/services/dp.py`'s own HOTFIX comment block
# names as the evidence the demotion guard rests on: "POST /api/route,
# Dallas -> Seattle, API default vehicle (10 mpg / 500 mi) -- exceeds
# GUNICORN_TIMEOUT=30 and returns HTTP 500. Reproduced 5/5 at
# 30.5s-35.7s." DELIBERATELY NOT the same cell `RECOVERY_PROBE_CELLS`' own
# `dallas_tx-seattle_wa` entry probes (that one is @1050mi at
# `ADMISSION_MANIFEST_VEHICLE`'s `starting_fuel=0.5`) -- Phase 25 D-16's
# evidence bar is scoped to THIS exact request shape, and
# `demotion_guard_rewrite_qualifies()` must be fed rows measured against
# it, never a different tank range or vehicle.
DEMOTION_BAR_PROBE_CELL = {
    "slug": "dallas_tx-seattle_wa",
    "label": "Dallas, TX -> Seattle, WA (API default vehicle)",
    "start": "32.7767,-96.7970",
    "finish": "47.6062,-122.3321",
    "tank_range_mi": Decimal(500),
    "vehicle": {"mpg": "10", "tank_range_mi": "500", "starting_fuel": "1"},
    "base_starting_fuel": Decimal("1"),
}


# --- DAILY_PER_IP_ROUTE_THROTTLE / VERDICT_PROBE_WORST_CASE_ATTEMPTS_PER_
# REPEAT / VERDICT_PROBE_MAX_REQUESTS (Phase 26 D-11, 2026-08-18) -----------
#
# A NEW, mode-scoped ceiling for the verdict sweep -- deliberately NOT a
# raise of LIVE_PROBE_MAX_REQUESTS. RESEARCH.md's own Pitfall 4
# recommends raising the existing constant; this plan deviates from that
# recommendation, because LIVE_PROBE_MAX_REQUESTS is also the exact budget
# `_enforce_budget()` checks the two EXISTING sweeps (the 2-cell main
# matrix, and RECOVERY_PROBE_CELLS) against -- raising it to fit the
# 26-cell verdict matrix would silently widen both of those unrelated
# guards' tolerance too. A mode-scoped constant keeps each sweep's own
# budget assertion exactly as strict as it was before this plan.
DAILY_PER_IP_ROUTE_THROTTLE = 200  # render.yaml ROUTE_THROTTLE_SUSTAINED_RATE

# `_measure_one_repeat`'s true per-repeat worst case is a primary attempt
# plus at most ONE retry, never more -- not `len(ladder)`. The legacy
# sweeps' `x len(ladder)` formula is a conservative superset of this bound
# at their (small) ladder sizes and is left unchanged.
VERDICT_PROBE_WORST_CASE_ATTEMPTS_PER_REPEAT = 2

# Round 1's worst case: 26 cells x VERDICT_PROBE_REPEATS(3) x
# VERDICT_PROBE_WORST_CASE_ATTEMPTS_PER_REPEAT(2) + 1 wake = 157 -- 78.5%
# of DAILY_PER_IP_ROUTE_THROTTLE (200/day, charged to the OPERATOR's own
# address, not to reviewer traffic), 5.2% of the deployment-wide
# MAPBOX_DAILY_CALL_CAP=3000 (nowhere near binding). Round 1's EXPECTED
# case (one attempt per repeat, no cache-hit retries) is 79. Round 2's own
# worst case -- four PROBE_SEAM_CACHE_BUST_FRACTIONS rungs x 3 repeats x 2
# attempts (=24), plus the DEMOTION_GUARD_PROBE_REPEATS=5 bar x 2 attempts
# (=10), plus a wake (=1) -- totals 35, independently well under 170.
# Each round enforces this SAME ceiling separately, at the point it runs;
# the two rounds are never summed into one combined budget check.
VERDICT_PROBE_MAX_REQUESTS = 170


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


class VerdictProbeMatrixGuardTests(SimpleTestCase):
    """Anti-vacuity guard for the widened 26-cell verdict matrix (Phase 26
    D-11): every acceptance criterion Tasks 1-3 name, covering the derived
    cell set, both fraction ladders, and the new mode-scoped request
    ceiling. Runs entirely offline, no network access."""

    # --- Task 1: VERDICT_PROBE_CELLS -----------------------------------

    def test_verdict_probe_cells_count_is_26(self):
        self.assertEqual(len(VERDICT_PROBE_CELLS), 26)

    def test_verdict_probe_cells_key_set_matches_admission_manifest_exactly(self):
        derived_keys = {
            (cell["slug"], int(cell["tank_range_mi"])) for cell in VERDICT_PROBE_CELLS
        }
        self.assertEqual(derived_keys, set(ADMISSION_MANIFEST))

    def test_every_cell_carries_the_full_field_shape(self):
        required_keys = {
            "slug",
            "label",
            "start",
            "finish",
            "waypoints",
            "tank_range_mi",
            "vehicle",
            "base_starting_fuel",
            "pre_phase_shipped_arm",
            "offline_admitted",
        }
        for cell in VERDICT_PROBE_CELLS:
            self.assertTrue(required_keys.issubset(cell.keys()))
            self.assertIsInstance(cell["waypoints"], tuple)

    def test_exactly_two_cells_are_demo_chips_and_the_multi_leg_one_has_one_waypoint(self):
        demo_slugs = {chip.slug for chip in DEMO_CHIPS}
        demo_cells = [cell for cell in VERDICT_PROBE_CELLS if cell["slug"] in demo_slugs]
        self.assertEqual(len(demo_cells), 2)
        self.assertEqual(sorted(len(cell["waypoints"]) for cell in demo_cells), [0, 1])

    def test_corridor_cells_use_the_manifest_pinned_starting_fuel(self):
        demo_slugs = {chip.slug for chip in DEMO_CHIPS}
        for cell in VERDICT_PROBE_CELLS:
            if cell["slug"] in demo_slugs:
                continue
            self.assertEqual(cell["base_starting_fuel"], STARTING_FUEL)
            self.assertEqual(
                Decimal(cell["vehicle"]["starting_fuel"]), cell["base_starting_fuel"]
            )

    def test_demo_chip_cells_use_the_demo_chip_vehicle(self):
        demo_slugs = {chip.slug for chip in DEMO_CHIPS}
        for cell in VERDICT_PROBE_CELLS:
            if cell["slug"] not in demo_slugs:
                continue
            self.assertEqual(cell["base_starting_fuel"], DEMO_CHIP_VEHICLE["starting_fuel"])
            self.assertEqual(Decimal(cell["vehicle"]["mpg"]), DEMO_CHIP_VEHICLE["mpg"])

    def test_verdict_probe_cells_is_produced_by_the_derivation_function(self):
        self.assertEqual(VERDICT_PROBE_CELLS, build_verdict_probe_cells())

    def test_verdict_probe_repeats_is_3(self):
        self.assertEqual(VERDICT_PROBE_REPEATS, 3)

    # --- Task 2: the two disjoint fraction ladders -----------------------

    def test_relative_cache_bust_reproduces_the_legacy_ladder_at_base_1(self):
        for value, fraction in zip(
            LIVE_PROBE_CACHE_BUST_LADDER, VERDICT_PROBE_CACHE_BUST_FRACTIONS
        ):
            self.assertEqual(relative_cache_bust_starting_fuel(Decimal(1), fraction), value)

    def test_verdict_ladder_yields_four_pairwise_distinct_values_at_base_half(self):
        values = {
            relative_cache_bust_starting_fuel(Decimal("0.5"), fraction)
            for fraction in VERDICT_PROBE_CACHE_BUST_FRACTIONS
        }
        self.assertEqual(len(values), 4)

    def test_probe_seam_ladder_yields_six_distinct_values_disjoint_from_verdict_ladder(self):
        verdict_values = {
            relative_cache_bust_starting_fuel(Decimal("0.5"), fraction)
            for fraction in VERDICT_PROBE_CACHE_BUST_FRACTIONS
        }
        seam_values = {
            relative_cache_bust_starting_fuel(Decimal("0.5"), fraction)
            for fraction in PROBE_SEAM_CACHE_BUST_FRACTIONS
        }
        self.assertEqual(len(seam_values), 6)
        self.assertEqual(verdict_values & seam_values, set())

    def test_fraction_ladders_are_disjoint_and_downward_only(self):
        self.assertEqual(
            set(VERDICT_PROBE_CACHE_BUST_FRACTIONS) & set(PROBE_SEAM_CACHE_BUST_FRACTIONS),
            set(),
        )
        for fraction in VERDICT_PROBE_CACHE_BUST_FRACTIONS + PROBE_SEAM_CACHE_BUST_FRACTIONS:
            self.assertGreater(fraction, 0)
            self.assertLess(fraction, 1)

    def test_ladder_lengths_match_their_repeat_counts_plus_one_spare(self):
        from routing.tests.test_dispatch_recovery import DEMOTION_GUARD_PROBE_REPEATS

        self.assertEqual(len(VERDICT_PROBE_CACHE_BUST_FRACTIONS), VERDICT_PROBE_REPEATS + 1)
        self.assertGreaterEqual(
            len(PROBE_SEAM_CACHE_BUST_FRACTIONS), DEMOTION_GUARD_PROBE_REPEATS + 1
        )

    # --- Task 3: the mode-scoped request ceiling -------------------------

    def test_round_1_worst_case_is_at_or_under_the_verdict_request_ceiling(self):
        worst_case = (
            26 * VERDICT_PROBE_REPEATS * VERDICT_PROBE_WORST_CASE_ATTEMPTS_PER_REPEAT + 1
        )
        self.assertLessEqual(worst_case, VERDICT_PROBE_MAX_REQUESTS)

    def test_verdict_ceiling_never_exceeds_the_per_ip_daily_throttle(self):
        self.assertLessEqual(VERDICT_PROBE_MAX_REQUESTS, DAILY_PER_IP_ROUTE_THROTTLE)

    def test_legacy_live_probe_max_requests_is_still_20(self):
        self.assertEqual(LIVE_PROBE_MAX_REQUESTS, 20)


class VerdictSweepGuardTests(SimpleTestCase):
    """Anti-vacuity guard for `probe_live_latency.py`'s Phase 26 D-12/D-13
    extensions (plan 26-04): the SHA-confirming wake, the widened
    `ProbeRow` fields, the per-cell-relative cache-bust branch on
    `_measure_one_repeat`, and the `--verdict` sweep itself. EVERY test
    below mocks the HTTP layer (`requests.get`/`requests.post`) and
    `time.sleep` -- this class issues ZERO live requests, per this
    plan's own scope fence (Round 1 runs in plan 26-05).

    `routing.management.commands.probe_live_latency` is imported LOCALLY
    inside each test method rather than at this module's top level, for
    the same reason `LiveLatencyProbeGuardTests` above already documents:
    that command module imports pinned constants FROM this module at ITS
    own top level, so a top-level import back into it here would be
    circular.
    """

    # --- shared fakes -----------------------------------------------

    @staticmethod
    def _fake_health_response(status_code=200, commit="deadbeef1234"):
        from unittest.mock import MagicMock

        response = MagicMock()
        response.status_code = status_code
        response.json.return_value = {"status": "ok", "commit": commit}
        return response

    @staticmethod
    def _fake_route_response(
        status_code=200,
        stages="solver;dur=5.0, total;dur=10.0",
        solver_strategy="exact_dp",
        price_index_status="current",
        fuel_stops=None,
        total_cost="123.45",
        json_data=None,
        json_raises=False,
    ):
        from unittest.mock import MagicMock

        response = MagicMock()
        response.status_code = status_code
        response.headers = {"Server-Timing": stages}
        if json_raises:
            response.json.side_effect = ValueError("bad json")
        elif json_data is not None:
            response.json.return_value = json_data
        else:
            response.json.return_value = {
                "solver_strategy": solver_strategy,
                "price_index_status": price_index_status,
                "total_cost": total_cost,
                "fuel_stops": fuel_stops if fuel_stops is not None else [{"opis_id": 1}],
                "map_url": "https://api.mapbox.com/directions/v5/mapbox/driving/...?access_token=pk.NOT_A_SENTINEL",
            }
        return response

    @staticmethod
    def _corridor_verdict_cell():
        demo_slugs = {chip.slug for chip in DEMO_CHIPS}
        return next(c for c in VERDICT_PROBE_CELLS if c["slug"] not in demo_slugs)

    @staticmethod
    def _demo_verdict_cell():
        demo_slugs = {chip.slug for chip in DEMO_CHIPS}
        return next(c for c in VERDICT_PROBE_CELLS if c["slug"] in demo_slugs)

    # --- Task 1: SHA-confirming wake + widened ProbeRow fields -----------

    def test_matching_commit_confirms_and_prints_the_sha(self):
        import io
        from unittest import mock

        from routing.management.commands.probe_live_latency import Command

        out = io.StringIO()
        command = Command(stdout=out)
        command._expect_commit = "deadbeef1234"
        response = self._fake_health_response(commit="deadbeef1234")
        with mock.patch(
            "routing.management.commands.probe_live_latency.requests.get",
            return_value=response,
        ):
            elapsed = command._wake()

        self.assertIsInstance(elapsed, float)
        self.assertEqual(command._confirmed_commit_sha, "deadbeef1234")
        self.assertIn("commit=deadbeef1234", out.getvalue())

    def test_mismatched_commit_raises_naming_both_shas(self):
        import io
        from unittest import mock

        from django.core.management.base import CommandError

        from routing.management.commands.probe_live_latency import Command

        command = Command(stdout=io.StringIO())
        command._expect_commit = "expectedsha0001"
        response = self._fake_health_response(commit="deployedsha9999")
        with mock.patch(
            "routing.management.commands.probe_live_latency.requests.get",
            return_value=response,
        ):
            with self.assertRaises(CommandError) as ctx:
                command._wake()

        message = str(ctx.exception)
        self.assertIn("expectedsha0001", message)
        self.assertIn("deployedsha9999", message)

    def test_missing_commit_field_raises(self):
        import io
        from unittest import mock
        from unittest.mock import MagicMock

        from django.core.management.base import CommandError

        from routing.management.commands.probe_live_latency import Command

        command = Command(stdout=io.StringIO())
        command._expect_commit = "whatever"
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"status": "ok"}  # no "commit" key
        with mock.patch(
            "routing.management.commands.probe_live_latency.requests.get",
            return_value=response,
        ):
            with self.assertRaises(CommandError):
                command._wake()

    def test_no_expect_commit_and_unresolvable_local_head_raises(self):
        import io
        from unittest import mock

        from django.core.management.base import CommandError

        from routing.management.commands.probe_live_latency import Command

        command = Command(stdout=io.StringIO())
        response = self._fake_health_response(commit="deployedsha9999")
        with mock.patch(
            "routing.management.commands.probe_live_latency.requests.get",
            return_value=response,
        ), mock.patch.object(Command, "_local_head_sha", return_value=None):
            with self.assertRaises(CommandError):
                command._wake()

    def test_local_head_sha_matches_git_rev_parse_head(self):
        import io
        import subprocess

        from routing.management.commands.probe_live_latency import Command

        command = Command(stdout=io.StringIO())
        expected = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        self.assertEqual(command._local_head_sha(), expected)

    def test_local_head_sha_returns_none_when_git_unavailable(self):
        import io
        from unittest import mock

        from routing.management.commands.probe_live_latency import Command

        command = Command(stdout=io.StringIO())
        with mock.patch(
            "routing.management.commands.probe_live_latency.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            self.assertIsNone(command._local_head_sha())

    def test_post_route_200_populates_price_index_status_stop_count_total_cost(self):
        import io

        from unittest import mock

        from routing.management.commands.probe_live_latency import Command

        command = Command(stdout=io.StringIO())
        response = self._fake_route_response(
            fuel_stops=[{"opis_id": 1}, {"opis_id": 2}],
            total_cost="199.50",
            price_index_status="stale",
        )
        with mock.patch(
            "routing.management.commands.probe_live_latency.requests.post",
            return_value=response,
        ):
            result = command._post_route(
                "32.7767,-96.7970", "47.6062,-122.3321", {"mpg": "10"}
            )
        (
            status,
            _elapsed,
            _stages,
            _strategy,
            error,
            price_index_status,
            stop_count,
            total_cost,
        ) = result
        self.assertEqual(status, 200)
        self.assertIsNone(error)
        self.assertEqual(price_index_status, "stale")
        self.assertEqual(stop_count, 2)
        self.assertEqual(total_cost, "199.50")

    def test_post_route_malformed_body_leaves_new_fields_none_without_raising(self):
        import io
        from unittest import mock

        from routing.management.commands.probe_live_latency import Command

        command = Command(stdout=io.StringIO())
        response = self._fake_route_response(json_raises=True)
        with mock.patch(
            "routing.management.commands.probe_live_latency.requests.post",
            return_value=response,
        ):
            result = command._post_route(
                "32.7767,-96.7970", "47.6062,-122.3321", {"mpg": "10"}
            )
        (
            status,
            _elapsed,
            _stages,
            strategy,
            error,
            price_index_status,
            stop_count,
            total_cost,
        ) = result
        self.assertEqual(status, 200)
        self.assertIsNone(error)
        self.assertIsNone(strategy)
        self.assertIsNone(price_index_status)
        self.assertIsNone(stop_count)
        self.assertIsNone(total_cost)

    def test_is_genuine_miss_definition_unaffected_by_the_new_fields(self):
        from routing.management.commands.probe_live_latency import ProbeRow

        row = ProbeRow(
            cell_label="x",
            repeat_index=0,
            ladder_value=Decimal("0.5"),
            status_code=200,
            wall_time_s=1.0,
            stages_ms={"solver": 5.0},
            censored=False,
            cache_hit=False,
            price_index_status="current",
            stop_count=2,
            total_cost="10.00",
            slug="x",
            tank_range_mi=Decimal(500),
            offline_admitted=True,
        )
        self.assertTrue(row.is_genuine_miss)

    def test_no_response_body_reaches_stdout(self):
        import io
        from unittest import mock

        from routing.management.commands.probe_live_latency import Command

        out = io.StringIO()
        command = Command(stdout=out)
        sentinel = "pk.SENTINEL_TOKEN_MUST_NOT_APPEAR"
        response = self._fake_route_response(
            json_data={
                "solver_strategy": "exact_dp",
                "price_index_status": "current",
                "total_cost": "10.00",
                "fuel_stops": [],
                "map_url": f"https://api.mapbox.com/directions/...?access_token={sentinel}",
            }
        )
        cell = {
            "slug": "x",
            "label": "X",
            "start": "0,0",
            "finish": "1,1",
            "vehicle": {"mpg": "10", "tank_range_mi": "500", "starting_fuel": "1"},
        }
        with mock.patch(
            "routing.management.commands.probe_live_latency.requests.post",
            return_value=response,
        ), mock.patch("routing.management.commands.probe_live_latency.time.sleep"):
            command._measure_one_repeat(cell, 0, len(LIVE_PROBE_CACHE_BUST_LADDER))
        self.assertNotIn(sentinel, out.getvalue())

    # --- Task 2: per-cell-relative cache-bust ----------------------------

    def test_legacy_cell_produces_absolute_ladder_values_in_order(self):
        import io
        from unittest import mock

        from routing.management.commands.probe_live_latency import Command

        command = Command(stdout=io.StringIO())
        cell = dict(LIVE_PROBE_CELLS[0])  # carries no base_starting_fuel
        seen_fuels = []

        def fake_post(url, **kwargs):
            seen_fuels.append(Decimal(kwargs["json"]["vehicle"]["starting_fuel"]))
            return self._fake_route_response()  # genuine miss -- no retry

        ladder_rungs = len(LIVE_PROBE_CACHE_BUST_LADDER)
        with mock.patch(
            "routing.management.commands.probe_live_latency.requests.post",
            side_effect=fake_post,
        ), mock.patch("routing.management.commands.probe_live_latency.time.sleep"):
            command._measure_one_repeat(cell, 0, ladder_rungs)
            command._measure_one_repeat(cell, 1, ladder_rungs)

        self.assertEqual(
            seen_fuels, [LIVE_PROBE_CACHE_BUST_LADDER[0], LIVE_PROBE_CACHE_BUST_LADDER[1]]
        )

    def test_corridor_verdict_cell_produces_four_distinct_relative_values_below_base(self):
        import io
        from unittest import mock

        from routing.management.commands.probe_live_latency import Command

        command = Command(stdout=io.StringIO())
        cell = self._corridor_verdict_cell()
        seen_fuels = []

        def fake_post(url, **kwargs):
            seen_fuels.append(Decimal(kwargs["json"]["vehicle"]["starting_fuel"]))
            return self._fake_route_response(stages="cache;dur=1.0")  # always a cache hit

        ladder_rungs = len(VERDICT_PROBE_CACHE_BUST_FRACTIONS)
        with mock.patch(
            "routing.management.commands.probe_live_latency.requests.post",
            side_effect=fake_post,
        ), mock.patch("routing.management.commands.probe_live_latency.time.sleep"):
            for repeat_index in range(VERDICT_PROBE_REPEATS):
                command._measure_one_repeat(
                    cell, repeat_index, ladder_rungs, ladder=VERDICT_PROBE_CACHE_BUST_FRACTIONS
                )

        self.assertEqual(len(set(seen_fuels)), 4)
        for value in seen_fuels:
            self.assertLess(value, cell["base_starting_fuel"])
            self.assertLessEqual(value, Decimal("0.6"))

    def test_demo_verdict_cell_produces_four_distinct_values_around_base_one(self):
        import io
        from unittest import mock

        from routing.management.commands.probe_live_latency import Command

        command = Command(stdout=io.StringIO())
        cell = self._demo_verdict_cell()
        seen_fuels = []

        def fake_post(url, **kwargs):
            seen_fuels.append(Decimal(kwargs["json"]["vehicle"]["starting_fuel"]))
            return self._fake_route_response(stages="cache;dur=1.0")  # always a cache hit

        ladder_rungs = len(VERDICT_PROBE_CACHE_BUST_FRACTIONS)
        with mock.patch(
            "routing.management.commands.probe_live_latency.requests.post",
            side_effect=fake_post,
        ), mock.patch("routing.management.commands.probe_live_latency.time.sleep"):
            for repeat_index in range(VERDICT_PROBE_REPEATS):
                command._measure_one_repeat(
                    cell, repeat_index, ladder_rungs, ladder=VERDICT_PROBE_CACHE_BUST_FRACTIONS
                )

        self.assertEqual(len(set(seen_fuels)), 4)
        for value in seen_fuels:
            self.assertLess(value, Decimal(1))
            self.assertGreater(value, Decimal("0.9"))

    def test_retry_fires_at_most_once_on_a_forced_double_cache_hit(self):
        import io
        from unittest import mock

        from routing.management.commands.probe_live_latency import Command

        command = Command(stdout=io.StringIO())
        cell = self._corridor_verdict_cell()
        call_count = {"n": 0}

        def fake_post(url, **kwargs):
            call_count["n"] += 1
            return self._fake_route_response(stages="cache;dur=1.0")  # always a cache hit

        ladder_rungs = len(VERDICT_PROBE_CACHE_BUST_FRACTIONS)
        with mock.patch(
            "routing.management.commands.probe_live_latency.requests.post",
            side_effect=fake_post,
        ), mock.patch("routing.management.commands.probe_live_latency.time.sleep"):
            row, attempts = command._measure_one_repeat(
                cell, 0, ladder_rungs, ladder=VERDICT_PROBE_CACHE_BUST_FRACTIONS
            )

        self.assertEqual(attempts, 2)
        self.assertEqual(call_count["n"], 2)
        self.assertTrue(row.cache_hit)

    def test_legacy_sweep_methods_signatures_unchanged_at_their_call_sites(self):
        """Structural pin: `_run_sweep`/`_run_recovery_sweep` must still
        call `_measure_one_repeat` with exactly the pre-Phase-26 three
        positional arguments, never passing `ladder=` themselves -- the
        one thing that makes the legacy absolute-ladder branch trigger
        by default rather than by an explicit override."""
        import ast
        import inspect

        from routing.management.commands import probe_live_latency

        source = inspect.getsource(probe_live_latency)
        tree = ast.parse(source)
        call_sites = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_measure_one_repeat"
        ]
        # _run_sweep, _run_recovery_sweep, _run_verdict_sweep,
        # _run_band_ladder_sweep, and _run_demotion_bar_sweep each call it
        # exactly once -- five call sites total (updated in plan 26-06,
        # which added the last two, both keyword call sites).
        self.assertEqual(len(call_sites), 5)
        legacy_call_sites = [c for c in call_sites if not c.keywords]
        self.assertEqual(len(legacy_call_sites), 2)
        for call in legacy_call_sites:
            self.assertEqual(len(call.args), 3)

    # --- Task 3: the --verdict sweep -------------------------------------

    def test_verdict_and_recovery_are_mutually_exclusive(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("probe_live_latency", "--verdict", "--recovery")

    def test_verdict_and_anomaly_are_mutually_exclusive(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("probe_live_latency", "--verdict", "--anomaly")

    def test_enforce_budget_default_ceiling_is_still_live_probe_max_requests(self):
        import io

        from django.core.management.base import CommandError

        from routing.management.commands.probe_live_latency import Command

        command = Command(stdout=io.StringIO())
        command._enforce_budget(LIVE_PROBE_MAX_REQUESTS, "test")  # must not raise
        with self.assertRaises(CommandError):
            command._enforce_budget(LIVE_PROBE_MAX_REQUESTS + 1, "test")

    def test_verdict_table_header_states_disagreement_is_expected(self):
        import io

        from routing.management.commands.probe_live_latency import Command, ProbeRow

        out = io.StringIO()
        command = Command(stdout=out)
        row = ProbeRow(
            cell_label="X",
            repeat_index=0,
            ladder_value=Decimal("0.5"),
            status_code=200,
            wall_time_s=1.0,
            stages_ms={"solver": 5.0, "total": 10.0},
            censored=False,
            cache_hit=False,
            solver_strategy="exact_dp",
            offline_admitted=True,
        )
        command._print_verdict_table([row])
        output = out.getvalue()
        self.assertIn("side by side and never reconciled", output)
        self.assertIn("expected", output.lower())
        self.assertIn("DispatchAdmissionManifestTests", output)

    def test_build_verdict_input_rows_carry_exactly_the_documented_keys(self):
        import io

        from routing.management.commands.probe_live_latency import Command, ProbeRow

        command = Command(stdout=io.StringIO())
        rows = [
            ProbeRow(
                cell_label=cell["label"],
                repeat_index=0,
                ladder_value=Decimal("0.5"),
                status_code=200,
                wall_time_s=1.0,
                stages_ms={"solver": 5.0},
                censored=False,
                cache_hit=False,
                solver_strategy="exact_dp",
                slug=cell["slug"],
                tank_range_mi=cell["tank_range_mi"],
                offline_admitted=cell["offline_admitted"],
            )
            for cell in VERDICT_PROBE_CELLS
        ]
        verdict_rows, floor_rows = command._build_verdict_input(rows)

        self.assertEqual(len(verdict_rows), 26)
        self.assertEqual(len(floor_rows), 26)
        expected_verdict_keys = {
            "slug",
            "tank_range_mi",
            "shipped_policy_strategy",
            "worst_of_repeats_solver_strategy",
            "worst_of_repeats_total_response_seconds",
        }
        expected_floor_keys = {
            "slug",
            "tank_range_mi",
            "confirmed_warm",
            "repeats",
            "all_repeats_genuine_cache_miss",
            "reported_figure_kind",
        }
        for row in verdict_rows:
            self.assertEqual(set(row.keys()), expected_verdict_keys)
        for row in floor_rows:
            self.assertEqual(set(row.keys()), expected_floor_keys)
            self.assertEqual(row["reported_figure_kind"], "worst")

    def test_verdict_sweep_all_clean_prints_both_arm_columns_and_reaches_verdict(self):
        import io
        from unittest import mock

        from routing.management.commands.probe_live_latency import Command

        out = io.StringIO()
        command = Command(stdout=out)

        def fake_wake(self_):
            self_._confirmed_commit_sha = "deadbeef1234"
            return 0.01

        def fake_post(url, **kwargs):
            return self._fake_route_response(
                solver_strategy="exact_dp",
                fuel_stops=[{"opis_id": 1}],
                total_cost="42.00",
                price_index_status="current",
            )

        with mock.patch.object(Command, "_wake", fake_wake), mock.patch(
            "routing.management.commands.probe_live_latency.requests.post",
            side_effect=fake_post,
        ), mock.patch("routing.management.commands.probe_live_latency.time.sleep"):
            verdict = command._run_verdict_sweep()

        output = out.getvalue()
        self.assertIn("26-CELL VERDICT TABLE", output)
        self.assertIn("offline_arm=", output)
        self.assertIn("live_arm=", output)
        self.assertIn("D-12 MEASUREMENT-FLOOR FACTS", output)
        self.assertIn("confirmed deployed commit SHA: deadbeef1234", output)
        self.assertIn("DISPATCH VERDICT:", output)
        self.assertIn(verdict, ("RECOVERED", "NOT RECOVERED"))

    def test_verdict_sweep_one_cache_hit_only_cell_raises_and_names_it(self):
        import io
        from unittest import mock

        from django.core.management.base import CommandError

        from routing.management.commands.probe_live_latency import Command

        out = io.StringIO()
        command = Command(stdout=out)
        bad_cell = VERDICT_PROBE_CELLS[0]

        def fake_wake(self_):
            self_._confirmed_commit_sha = "deadbeef1234"
            return 0.01

        def fake_post(url, **kwargs):
            body = kwargs["json"]
            if body["start"] == bad_cell["start"] and body["finish"] == bad_cell["finish"]:
                return self._fake_route_response(stages="cache;dur=1.0")  # never a genuine miss
            return self._fake_route_response(
                solver_strategy="exact_dp",
                fuel_stops=[{"opis_id": 1}],
                total_cost="10.00",
                price_index_status="current",
            )

        with mock.patch.object(Command, "_wake", fake_wake), mock.patch(
            "routing.management.commands.probe_live_latency.requests.post",
            side_effect=fake_post,
        ), mock.patch("routing.management.commands.probe_live_latency.time.sleep"):
            with self.assertRaises(CommandError):
                command._run_verdict_sweep()

        output = out.getvalue()
        self.assertIn(bad_cell["slug"], output)
        self.assertNotIn("DISPATCH VERDICT:", output)


class BandLadderProbeGuardTests(RealCorridorDispatchTestCase):
    """Anti-vacuity guard for the D-05/D-06 gated-probe extensions (plan
    26-06): `BAND_LADDER_RUNG_CELLS`' rung-to-cell mapping, the
    `--band-ladder`/`--demotion-bar` sweeps, and the
    `DISPATCH_PROBE_SECRET` environment gate. Every HTTP-issuing test
    below mocks `requests.get`/`requests.post` and `time.sleep` -- ZERO
    live requests are ever issued here; this plan wires the seam, it does
    not run Round 2's actual measurement.

    Subclasses `RealCorridorDispatchTestCase` (DB-backed) rather than
    `SimpleTestCase`, because the rung-to-band-membership test below needs
    the real seeded station dataset to compute a genuine
    `dp.estimate_transition_count` -- the same reproduction pattern
    `DispatchAdmissionManifestTests` (`test_solver_dispatch.py`) already
    uses for `ADMISSION_MANIFEST` itself.
    """

    @staticmethod
    def _fake_route_response(status_code=200, stages="solver;dur=5.0, total;dur=10.0"):
        from unittest.mock import MagicMock

        response = MagicMock()
        response.status_code = status_code
        response.headers = {"Server-Timing": stages}
        response.json.return_value = {
            "solver_strategy": "exact_dp",
            "price_index_status": "current",
            "total_cost": "10.00",
            "fuel_stops": [],
            "map_url": "https://api.mapbox.com/directions/...",
        }
        return response

    @staticmethod
    def _fake_wake():
        def fake_wake(self_):
            self_._confirmed_commit_sha = "deadbeef1234"
            return 0.01

        return fake_wake

    # --- BAND_LADDER_RUNG_CELLS mapping -----------------------------

    def test_rung_to_cell_mapping_covers_every_real_rung_above_shipped_budget(self):
        from routing.tests.test_dispatch_recovery import DP_TRANSITION_BUDGET_LADDER

        expected_rungs = {
            rung
            for rung in DP_TRANSITION_BUDGET_LADDER
            if rung is not None and rung > dp.DP_TRANSITION_BUDGET
        }
        self.assertEqual(set(BAND_LADDER_RUNG_CELLS.keys()), expected_rungs)

    def test_each_mapped_cells_estimate_falls_inside_its_rungs_band(self):
        """Reproduces each mapped cell's REAL `estimate_transition_count`
        over the pruned search set -- the same computation
        `DispatchAdmissionManifestTests` uses to reproduce
        `ADMISSION_MANIFEST` -- and asserts it falls strictly inside the
        band its own rung newly opens: greater than the previous rung
        (the estimate was NOT already admitted there) and at or under
        this rung (the estimate IS newly admitted here)."""
        from routing.tests.test_dispatch_recovery import DP_TRANSITION_BUDGET_LADDER

        non_none = [r for r in DP_TRANSITION_BUDGET_LADDER if r is not None]
        for rung, cell in BAND_LADDER_RUNG_CELLS.items():
            route, candidates = self._route_and_candidates(cell["slug"])
            tank = cell["tank_range_mi"]
            search_set = prune_dominated_candidates(
                candidates, tank_range_mi=tank, total_route_mi=route.total_route_mi
            )
            estimate = dp.estimate_transition_count(
                search_set,
                total_route_mi=route.total_route_mi,
                tank_range_mi=tank,
                starting_fuel=Decimal(cell["vehicle"]["starting_fuel"]),
            )
            rung_index = non_none.index(rung)
            lower_bound = (
                non_none[rung_index - 1] if rung_index > 0 else dp.DP_TRANSITION_BUDGET
            )
            with self.subTest(rung=rung, slug=cell["slug"]):
                self.assertGreater(
                    estimate,
                    lower_bound,
                    f"{cell['slug']}@{tank}mi estimate {estimate} does not "
                    f"exceed the lower bound {lower_bound} for rung {rung} "
                    "-- this cell is not NEWLY admitted here.",
                )
                self.assertLessEqual(
                    estimate,
                    rung,
                    f"{cell['slug']}@{tank}mi estimate {estimate} exceeds "
                    f"rung {rung} -- this cell is not admitted by this "
                    "rung at all.",
                )

    # --- --band-ladder sweep -----------------------------------------

    def test_mocked_band_ladder_run_rows_match_contract_and_hand_computed_ceiling(self):
        import io
        from unittest import mock

        from routing.management.commands.probe_live_latency import Command
        from routing.tests.test_dispatch_recovery import derive_band_ceiling

        # Deliberately chosen so qualification is genuinely mixed (not
        # every rung, not none): budget_limit = 512 * 0.50 = 256,
        # peak_rss_mb * ROUTE_ALTERNATIVES_FANOUT(3) <= 256 requires
        # peak_rss_mb <= 85.33 -- 70,000 and 130,000 qualify, 200,000 and
        # 400,000 do not, so the hand-computed ceiling is 130,000, not
        # the trivial largest-or-smallest rung.
        peak_by_rung = {70_000: "10.0", 130_000: "40.0", 200_000: "90.0", 400_000: "300.0"}

        def fake_post(url, **kwargs):
            rung = int(kwargs["headers"][PROBE_BUDGET_HEADER])
            self.assertEqual(kwargs["headers"][PROBE_KEY_HEADER], "test-secret")
            peak = peak_by_rung[rung]
            return self._fake_route_response(
                stages=f"solver;dur=5.0, peak_rss_mb;dur={peak}, total;dur=10.0"
            )

        out = io.StringIO()
        command = Command(stdout=out)
        captured = {}

        def fake_derive(rows):
            captured["rows"] = list(rows)
            return derive_band_ceiling(rows)

        with mock.patch.dict(os.environ, {"DISPATCH_PROBE_SECRET": "test-secret"}), \
            mock.patch.object(Command, "_wake", self._fake_wake()), \
            mock.patch(
                "routing.management.commands.probe_live_latency.requests.post",
                side_effect=fake_post,
            ), mock.patch(
                "routing.management.commands.probe_live_latency.time.sleep"
            ), mock.patch(
                "routing.management.commands.probe_live_latency.derive_band_ceiling",
                side_effect=fake_derive,
            ):
            ceiling = command._run_band_ladder_sweep()

        self.assertEqual(len(captured["rows"]), 4)
        for row in captured["rows"]:
            self.assertEqual(set(row.keys()), {"rung", "peak_rss_mb"})
            self.assertIsInstance(row["peak_rss_mb"], Decimal)
        self.assertEqual(ceiling, 130_000)
        self.assertIn("DERIVED BAND_CEILING: 130000", out.getvalue())

    def test_mocked_band_ladder_run_with_one_rung_missing_peak_rss_mb_is_unmeasured_and_excluded(
        self,
    ):
        import io
        from unittest import mock

        from routing.management.commands.probe_live_latency import Command
        from routing.tests.test_dispatch_recovery import derive_band_ceiling

        peak_by_rung = {70_000: "10.0", 130_000: "40.0", 200_000: "90.0"}

        def fake_post(url, **kwargs):
            rung = int(kwargs["headers"][PROBE_BUDGET_HEADER])
            if rung == 400_000:
                # Simulates the RSS reader returning None (e.g. off-Linux)
                # -- no peak_rss_mb token in Server-Timing at all.
                return self._fake_route_response(stages="solver;dur=5.0, total;dur=10.0")
            peak = peak_by_rung[rung]
            return self._fake_route_response(
                stages=f"solver;dur=5.0, peak_rss_mb;dur={peak}, total;dur=10.0"
            )

        out = io.StringIO()
        command = Command(stdout=out)
        captured = {}

        def fake_derive(rows):
            captured["rows"] = list(rows)
            return derive_band_ceiling(rows)

        with mock.patch.dict(os.environ, {"DISPATCH_PROBE_SECRET": "test-secret"}), \
            mock.patch.object(Command, "_wake", self._fake_wake()), \
            mock.patch(
                "routing.management.commands.probe_live_latency.requests.post",
                side_effect=fake_post,
            ), mock.patch(
                "routing.management.commands.probe_live_latency.time.sleep"
            ), mock.patch(
                "routing.management.commands.probe_live_latency.derive_band_ceiling",
                side_effect=fake_derive,
            ):
            command._run_band_ladder_sweep()

        self.assertEqual(len(captured["rows"]), 3)
        self.assertNotIn(400_000, [row["rung"] for row in captured["rows"]])
        self.assertIn("rung=400000", out.getvalue())
        self.assertIn("UNMEASURED", out.getvalue())

    # --- --demotion-bar sweep -----------------------------------------

    def test_mocked_demotion_bar_run_emits_exactly_five_rows_with_the_five_required_keys(self):
        import io
        from unittest import mock

        from routing.management.commands.probe_live_latency import Command
        from routing.tests.test_dispatch_recovery import (
            DEMOTION_GUARD_PROBE_REPEATS,
            demotion_guard_rewrite_qualifies,
        )

        def fake_post(url, **kwargs):
            self.assertEqual(kwargs["headers"][PROBE_BUDGET_HEADER], "70000")
            self.assertEqual(kwargs["headers"][PROBE_KEY_HEADER], "test-secret")
            return self._fake_route_response(stages="solver;dur=500.0, total;dur=600.0")

        out = io.StringIO()
        command = Command(stdout=out)
        captured = {}

        def fake_qualifies(rows):
            captured["rows"] = list(rows)
            return demotion_guard_rewrite_qualifies(rows)

        with mock.patch.dict(os.environ, {"DISPATCH_PROBE_SECRET": "test-secret"}), \
            mock.patch.object(Command, "_wake", self._fake_wake()), \
            mock.patch(
                "routing.management.commands.probe_live_latency.requests.post",
                side_effect=fake_post,
            ), mock.patch(
                "routing.management.commands.probe_live_latency.time.sleep"
            ), mock.patch(
                "routing.management.commands.probe_live_latency.demotion_guard_rewrite_qualifies",
                side_effect=fake_qualifies,
            ):
            qualifies = command._run_demotion_bar_sweep(70_000)

        rows = captured["rows"]
        self.assertEqual(len(rows), DEMOTION_GUARD_PROBE_REPEATS)
        expected_keys = {"source", "host", "cache_miss", "status_code", "solver_stage_seconds"}
        for row in rows:
            self.assertEqual(set(row.keys()), expected_keys)
        self.assertTrue(qualifies)
        self.assertIn("DEMOTION-GUARD REWRITE QUALIFIES: True", out.getvalue())

    def test_demotion_bar_run_with_one_cache_hit_repeat_still_emits_five_rows_and_fails(self):
        """The failing repeat's REAL row (cache_miss=False) is passed
        through, never synthesised as a passing one -- the pinned
        `demotion_guard_rewrite_qualifies` is what returns `False`."""
        import io
        from unittest import mock

        from routing.management.commands.probe_live_latency import Command
        from routing.tests.test_dispatch_recovery import (
            DEMOTION_GUARD_PROBE_REPEATS,
            demotion_guard_rewrite_qualifies,
        )

        call_count = {"n": 0}

        def fake_post(url, **kwargs):
            call_count["n"] += 1
            # Repeat 0's BOTH attempts (the primary at rung_index=0 and
            # its one permitted retry at rung_index=1) return a cache
            # hit, so _measure_one_repeat exhausts its retry and returns
            # a row with cache_hit=True (never a genuine miss). Every
            # later repeat's single attempt returns a genuine miss.
            if call_count["n"] in (1, 2):
                return self._fake_route_response(stages="cache;dur=1.0")  # cache hit
            return self._fake_route_response(stages="solver;dur=500.0, total;dur=600.0")

        out = io.StringIO()
        command = Command(stdout=out)
        captured = {}

        def fake_qualifies(rows):
            captured["rows"] = list(rows)
            return demotion_guard_rewrite_qualifies(rows)

        with mock.patch.dict(os.environ, {"DISPATCH_PROBE_SECRET": "test-secret"}), \
            mock.patch.object(Command, "_wake", self._fake_wake()), \
            mock.patch(
                "routing.management.commands.probe_live_latency.requests.post",
                side_effect=fake_post,
            ), mock.patch(
                "routing.management.commands.probe_live_latency.time.sleep"
            ), mock.patch(
                "routing.management.commands.probe_live_latency.demotion_guard_rewrite_qualifies",
                side_effect=fake_qualifies,
            ):
            qualifies = command._run_demotion_bar_sweep(70_000)

        self.assertEqual(len(captured["rows"]), DEMOTION_GUARD_PROBE_REPEATS)
        self.assertFalse(captured["rows"][0]["cache_miss"])
        self.assertFalse(qualifies)

    # --- missing-secret environment gate --------------------------------

    def test_missing_secret_environment_variable_raises_command_error_for_both_modes(self):
        import io
        from unittest import mock

        from django.core.management.base import CommandError

        from routing.management.commands.probe_live_latency import Command

        with mock.patch.dict(os.environ):
            os.environ.pop("DISPATCH_PROBE_SECRET", None)

            out = io.StringIO()
            command = Command(stdout=out)
            with mock.patch.object(Command, "_wake", self._fake_wake()):
                with self.assertRaises(CommandError) as ctx:
                    command._run_band_ladder_sweep()
                self.assertIn("DISPATCH_PROBE_SECRET", str(ctx.exception))
                self.assertNotIn("DISPATCH_PROBE_SECRET", out.getvalue())

                out2 = io.StringIO()
                command2 = Command(stdout=out2)
                with self.assertRaises(CommandError) as ctx2:
                    command2._run_demotion_bar_sweep(70_000)
                self.assertIn("DISPATCH_PROBE_SECRET", str(ctx2.exception))

    # --- mode plumbing --------------------------------------------------

    def test_band_ladder_and_demotion_bar_are_mutually_exclusive(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("probe_live_latency", "--band-ladder", "--demotion-bar")

    def test_band_ladder_and_verdict_are_mutually_exclusive(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("probe_live_latency", "--band-ladder", "--verdict")

    def test_demotion_bar_without_rung_raises_command_error(self):
        import io
        from unittest import mock

        from django.core.management.base import CommandError

        from routing.management.commands.probe_live_latency import Command

        command = Command(stdout=io.StringIO())
        with mock.patch.dict(os.environ, {"DISPATCH_PROBE_SECRET": "test-secret"}):
            with self.assertRaises(CommandError) as ctx:
                command._run_demotion_bar_sweep(None)
        self.assertIn("--rung", str(ctx.exception))

    def test_demotion_bar_with_off_ladder_rung_raises_command_error(self):
        import io
        from unittest import mock

        from django.core.management.base import CommandError

        from routing.management.commands.probe_live_latency import Command

        command = Command(stdout=io.StringIO())
        with mock.patch.dict(os.environ, {"DISPATCH_PROBE_SECRET": "test-secret"}):
            with self.assertRaises(CommandError):
                command._run_demotion_bar_sweep(12_345)
