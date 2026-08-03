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

The verdict itself is recorded by a later commit (task 3), appended below
this section once measured -- never edited into this hypothesis text.
"""
import os
from decimal import Decimal

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
