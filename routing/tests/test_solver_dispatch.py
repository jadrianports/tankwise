"""Tests for `solver.solve()`'s DP-vs-greedy dispatch (Phase 18-04c): the
deterministic pre-flight transition-count estimate, its calibrated
threshold (`dp.DP_TRANSITION_BUDGET`), and which strategy each real
corridor/tank-range cell chooses.

`RealCorridorDispatchTestCase` seeds the actual, committed station
dataset (`data/stations_geocoded.csv`, the same file production seeds
from) into the test database once per test class via `setUpTestData`, so
these tests exercise the real `corridor.candidates()` DB path against the
real, committed corridor geometry fixtures
(`routing.tests.test_corridor_fixtures`) -- not a synthetic stand-in for
either.
"""
import io
import time
from decimal import Decimal

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from routing.services import corridor, dp
from routing.services.exceptions import InfeasibleRouteError
from routing.services.prune import prune_dominated_candidates
from routing.services.solver import Candidate, SolverStrategy, solve
from routing.tests.test_corridor_fixtures import (
    CORRIDORS,
    DEMO_CHIP_VEHICLE,
    DEMO_CHIPS,
    TANK_RANGES_MI,
    factor_lookup_for_basis,
    load_corridor_route,
    load_demo_chip_route,
)

MPG = Decimal(10)
STARTING_FUEL = Decimal("0.5")
PENALTY = Decimal(35)

# The twelve corridor/tank-range cells 18-04c-SUMMARY.md's calibration
# table measures -- the same cells `dp.DP_TRANSITION_BUDGET`'s own
# calibration comment cites. Not the full 12-corridor x 2-tank-range
# cross product: these are exactly the pinned cells the calibration was
# performed against.
CALIBRATION_CELLS = (
    ("toronto_oh-hillsboro_or", Decimal(1050)),
    ("toronto_oh-hillsboro_or", Decimal(500)),
    ("el_paso_tx-portland_me", Decimal(1050)),
    ("el_paso_tx-portland_me", Decimal(500)),
    ("jacksonville_fl-bangor_me", Decimal(500)),
    ("miami_fl-boston_ma", Decimal(500)),
    ("atlanta_ga-denver_co", Decimal(500)),
    ("dallas_tx-seattle_wa", Decimal(1050)),
    ("dallas_tx-seattle_wa", Decimal(500)),
    ("san_diego_ca-jacksonville_fl", Decimal(500)),
    ("phoenix_az-minneapolis_mn", Decimal(1050)),
    ("houston_tx-chicago_il", Decimal(1050)),
)

# The design target is <=5s per DP-path cell (comfortably inside the
# deployed 30s gunicorn worker timeout). This test's own assertion uses a
# generous 3x ceiling on top of that, not the bare 5s figure itself, for
# the same reason `test_corridor_fixtures.py`'s own
# `_MILEAGE_TOLERANCE_FRACTION`/`_MIN_COORDINATE_COUNT` are loose sanity
# bands rather than tight numbers: this machine's own measured worst
# DP-path cell was 3.087s (see `dp.DP_TRANSITION_BUDGET`'s calibration
# comment), and a slower CI/production box must not turn a real,
# non-regressing run into a flaky failure. 15s is still more than an
# order of magnitude below the multi-tens-of-seconds a mis-routed heavy
# corridor (e.g. a disabled or mis-calibrated dispatch) would actually
# take, so it stays a meaningful regression guard, not a rubber stamp.
LATENCY_CEILING_S = 15


class RealCorridorDispatchTestCase(TestCase):
    """Seeds the real station dataset once per class -- `setUpTestData`
    runs inside one wrapping transaction/savepoint Django reuses across
    every test method in the class, so the (bulk_create-based) seed only
    ever runs once regardless of how many tests import from it."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_stations", stdout=io.StringIO())
        corridor.warm_index()

    def setUp(self):
        self.factor_for = factor_lookup_for_basis("neutral")

    def _route_and_candidates(self, slug):
        route = load_corridor_route(slug)
        candidates = corridor.candidates(route, factor_for=self.factor_for)
        return route, candidates

    def _solve(self, slug, tank_range_mi):
        route, candidates = self._route_and_candidates(slug)
        return solve(
            candidates,
            route.total_route_mi,
            tank_range_mi=tank_range_mi,
            mpg=MPG,
            starting_fuel=STARTING_FUEL,
            penalty=PENALTY,
        )


class HeavyLightDispatchTests(RealCorridorDispatchTestCase):
    """Design requirement 5's first test: a heavy real corridor takes the
    penalty-aware heuristic path, a light one takes the DP path."""

    def test_heavy_corridor_takes_the_penalty_aware_heuristic(self):
        plan = self._solve("toronto_oh-hillsboro_or", Decimal(1050))
        self.assertEqual(plan.strategy, SolverStrategy.PENALTY_AWARE_HEURISTIC)
        self.assertGreater(len(plan.stops), 0)

    def test_light_corridor_takes_the_exact_dp(self):
        # Corridor changed 2026-08-02 from dallas_tx-seattle_wa, which is no
        # longer a "light" corridor under the hotfixed
        # DP_TRANSITION_BUDGET=50,000: its estimate is 117,852 @1050mi and
        # 61,912 @500mi, so it now dispatches to the heuristic. That demotion
        # is the whole point of the hotfix -- Dallas -> Seattle at the API
        # default vehicle was returning HTTP 500 live against
        # GUNICORN_TIMEOUT=30 (see dp.py's DP_TRANSITION_BUDGET note).
        #
        # san_diego_ca-jacksonville_fl @1050mi is the replacement: estimate
        # 15,724, comfortably under the budget, but a genuinely substantial
        # corridor (312 raw candidates, 39 retained after the prune) rather
        # than a trivially small one -- so this test still exercises a real
        # exact-DP dispatch instead of passing vacuously on a toy input.
        plan = self._solve("san_diego_ca-jacksonville_fl", Decimal(1050))
        self.assertEqual(plan.strategy, SolverStrategy.EXACT_DP)
        self.assertGreater(len(plan.stops), 0)


class DispatchDeterminismTests(RealCorridorDispatchTestCase):
    """Design requirement 5's second test: the strategy decision (and the
    plan it produces) is deterministic across repeat calls on identical
    input -- load-bearing for the response cache, since `solve()`'s
    dispatch is never re-keyed separately (see `routing/cache.py`)."""

    def test_repeat_solve_calls_choose_the_same_strategy_and_plan(self):
        route, candidates = self._route_and_candidates("toronto_oh-hillsboro_or")
        kwargs = dict(
            tank_range_mi=Decimal(500),
            mpg=MPG,
            starting_fuel=STARTING_FUEL,
            penalty=PENALTY,
        )
        plan_a = solve(candidates, route.total_route_mi, **kwargs)
        plan_b = solve(candidates, route.total_route_mi, **kwargs)

        self.assertEqual(plan_a.strategy, plan_b.strategy)
        self.assertEqual(plan_a.total_cost, plan_b.total_cost)
        self.assertEqual(plan_a.total_gallons, plan_b.total_gallons)
        self.assertEqual(
            [(s.opis_id, s.gallons, s.cost) for s in plan_a.stops],
            [(s.opis_id, s.gallons, s.cost) for s in plan_b.stops],
        )

    def test_repeat_solve_calls_across_every_calibration_cell_agree(self):
        for slug, tank_range_mi in CALIBRATION_CELLS:
            with self.subTest(slug=slug, tank_range_mi=tank_range_mi):
                route, candidates = self._route_and_candidates(slug)
                kwargs = dict(
                    tank_range_mi=tank_range_mi,
                    mpg=MPG,
                    starting_fuel=STARTING_FUEL,
                    penalty=PENALTY,
                )
                plan_a = solve(candidates, route.total_route_mi, **kwargs)
                plan_b = solve(candidates, route.total_route_mi, **kwargs)
                self.assertEqual(plan_a.strategy, plan_b.strategy)
                self.assertEqual(plan_a.total_cost, plan_b.total_cost)


class EstimateIsAPureFunctionTests(SimpleTestCase):
    """`dp.estimate_transition_count` itself is a pure function of its
    arguments -- no DB, no candidates.py, no wall clock -- verified
    directly against synthetic input so this property does not depend on
    the real dataset staying byte-identical over time."""

    def _candidates(self):
        return [
            Candidate(
                name=f"S{i}",
                opis_id=i,
                price_per_gallon=Decimal("2.50") + Decimal(i) / Decimal(100),
                distance_from_start_mi=Decimal(i * 37),
            )
            for i in range(1, 60)
        ]

    def test_same_input_produces_the_same_estimate_every_time(self):
        candidates = self._candidates()
        kwargs = dict(
            total_route_mi=Decimal(2200),
            tank_range_mi=Decimal(500),
            starting_fuel=Decimal("0.5"),
        )
        results = {
            dp.estimate_transition_count(candidates, **kwargs) for _ in range(10)
        }
        self.assertEqual(len(results), 1)

    def test_estimate_is_independent_of_input_list_order(self):
        candidates = self._candidates()
        kwargs = dict(
            total_route_mi=Decimal(2200),
            tank_range_mi=Decimal(500),
            starting_fuel=Decimal("0.5"),
        )
        forward = dp.estimate_transition_count(candidates, **kwargs)
        reversed_order = dp.estimate_transition_count(
            list(reversed(candidates)), **kwargs
        )
        self.assertEqual(forward, reversed_order)

    def test_empty_candidates_returns_a_small_nonnegative_estimate(self):
        estimate = dp.estimate_transition_count(
            [],
            total_route_mi=Decimal(500),
            tank_range_mi=Decimal(500),
            starting_fuel=Decimal(1),
        )
        self.assertGreaterEqual(estimate, 0)
        self.assertLessEqual(estimate, dp.DP_TRANSITION_BUDGET)


class CorridorLatencyGuardTests(RealCorridorDispatchTestCase):
    """Design requirement 5's third test: every one of the twelve
    corridor/tank-range calibration cells completes under a generous
    latency ceiling through the real `solve()` dispatch, regardless of
    which strategy it chooses. See `LATENCY_CEILING_S`'s own comment for
    why this is a loose, non-flaky ceiling rather than the bare 5s design
    target."""

    def test_every_calibration_cell_completes_under_the_latency_ceiling(self):
        for slug, tank_range_mi in CALIBRATION_CELLS:
            with self.subTest(slug=slug, tank_range_mi=tank_range_mi):
                route, candidates = self._route_and_candidates(slug)
                start = time.perf_counter()
                plan = solve(
                    candidates,
                    route.total_route_mi,
                    tank_range_mi=tank_range_mi,
                    mpg=MPG,
                    starting_fuel=STARTING_FUEL,
                    penalty=PENALTY,
                )
                elapsed = time.perf_counter() - start
                self.assertLessEqual(
                    elapsed,
                    LATENCY_CEILING_S,
                    f"{slug} @{tank_range_mi}mi took {elapsed:.3f}s via "
                    f"{plan.strategy}, exceeding the {LATENCY_CEILING_S}s "
                    "ceiling",
                )


class DeployedHardwareDispatchTests(RealCorridorDispatchTestCase):
    """Plan 18-12, Task 2 -- per-cell dispatch assertions against plan
    18-11's DEPLOYED-hardware measurement, at the API-default vehicle
    (``solve()``'s own defaults: mpg=10, tank_range_mi=500,
    starting_fuel=1), the exact vehicle 18-11's live probe used -- NOT
    ``CALIBRATION_CELLS``' own starting_fuel=0.5 vehicle.

    18-10's own qualifying predictor shortlist is EMPTY (see ``dp.py``'s
    "Why the transition-count estimate was not replaced" docstring
    section), so this plan changes no dispatch code. This test class pins,
    as a permanent regression guard, the CURRENT (unchanged) policy's
    classification against real deployed evidence, so a future edit that
    silently changes it is caught here rather than discovered live again.
    """

    def test_dallas_seattle_matches_18_11s_live_heuristic_classification(self):
        route, candidates = self._route_and_candidates("dallas_tx-seattle_wa")
        plan = solve(candidates, route.total_route_mi)  # API defaults
        self.assertEqual(plan.strategy, SolverStrategy.PENALTY_AWARE_HEURISTIC)

    def test_sacramento_slc_matches_18_11s_live_exact_dp_classification(self):
        route, candidates = self._route_and_candidates(
            "sacramento_ca-salt_lake_city_ut"
        )
        plan = solve(candidates, route.total_route_mi)  # API defaults
        self.assertEqual(plan.strategy, SolverStrategy.EXACT_DP)


class DispatchDemotionGuardTests(RealCorridorDispatchTestCase):
    """Plan 18-12, Task 2's demotion guard (T-18-47's second half): every
    cell 18-11 measured or recorded as breaching must NOT reach
    ``exact_dp``. Paired with ``DispatchRetentionFloorGuardTests`` below --
    either guard alone is satisfiable vacuously (a demote-everything policy
    passes this one trivially; a retain-everything policy would pass that
    one trivially); together they are not.
    """

    def test_known_live_breaching_cell_does_not_reach_exact_dp(self):
        route, candidates = self._route_and_candidates("dallas_tx-seattle_wa")
        plan = solve(candidates, route.total_route_mi)  # API defaults: 500mi tank
        self.assertNotEqual(
            plan.strategy,
            SolverStrategy.EXACT_DP,
            "dallas_tx-seattle_wa@500mi (API-default vehicle) reproduced "
            "HTTP 500 5/5 at 30.5-35.7s live under exact_dp pre-hotfix "
            "(18-VERIFICATION.md, commit 8946567) -- it must never "
            "dispatch to exact_dp again.",
        )


class DispatchRetentionFloorGuardTests(RealCorridorDispatchTestCase):
    """Plan 18-12, Task 2's anti-vacuity retention guard (T-18-47's first
    half). ``dp.DISPATCH_RETENTION_FLOOR`` (imported, never redeclared)
    names the full set of cells with genuine deployed-hardware ``exact_dp``
    live-fine evidence: ``sacramento_ca-salt_lake_city_ut`` (estimate ~120)
    and ``dallas_tx-seattle_wa``@1050mi (estimate 117,852). The one known
    live-breaching cell, ``dallas_tx-seattle_wa``@500mi, estimates 61,912 --
    SMALLER than the retention-floor cell it would need to coexist with
    under a single threshold, which is exactly why ``dp.py``'s own pinned
    comment proves NOT TRACTABLE: no budget can retain both floor cells
    while also demoting the breaching one.

    Three assertions, none satisfiable vacuously by construction:

    1. This test's own retention-evidence set tracks
       ``dp.DISPATCH_RETENTION_FLOOR`` in SIZE -- so a future change to the
       constant cannot silently drift apart from what this test actually
       checks.
    2. The inversion the NOT TRACTABLE verdict depends on is still
       present: the breaching cell's estimate is still smaller than the
       retention-floor cell's estimate. If this ever flips, the verdict
       must be re-derived, not assumed still true.
    3. **Updated 2026-08-04 (plan 18.1-08):** the dispatch policy now
       retains ``exact_dp`` on BOTH floor cells, not just the achievable
       maximum of one. This does not overturn point 2's inversion or
       ``dp.py``'s NOT TRACTABLE proof about a single ESTIMATE threshold
       with no clock behind it -- that proof still holds exactly as
       stated for that narrower question. What changed is that
       ``DP_TRANSITION_BUDGET`` was raised behind a wall-clock deadline
       (D-01's second layer), so the estimate no longer has to demote
       ``dallas_tx-seattle_wa``@1050mi on its own to protect the worker;
       it only has to admit the attempt, and the clock (not the
       threshold) bounds it. Original reasoning kept on the record
       above, not deleted: until this plan, "the achievable maximum of
       the floor set" was exactly one cell, and that was true and
       provable under the single-threshold policy this test originally
       exercised.
    """

    _RETENTION_SET = (
        ("sacramento_ca-salt_lake_city_ut", Decimal(500)),
        ("dallas_tx-seattle_wa", Decimal(1050)),
    )

    def test_retention_set_size_matches_the_pinned_floor(self):
        self.assertEqual(
            len(self._RETENTION_SET),
            dp.DISPATCH_RETENTION_FLOOR,
            "this test's own retention-evidence set must track "
            "dp.DISPATCH_RETENTION_FLOOR exactly -- imported, never "
            "redeclared, so the two cannot silently drift apart.",
        )

    def test_the_inversion_the_not_tractable_verdict_depends_on_still_holds(self):
        route, candidates = self._route_and_candidates("dallas_tx-seattle_wa")
        search_500 = prune_dominated_candidates(
            candidates,
            tank_range_mi=Decimal(500),
            total_route_mi=route.total_route_mi,
        )
        search_1050 = prune_dominated_candidates(
            candidates,
            tank_range_mi=Decimal(1050),
            total_route_mi=route.total_route_mi,
        )
        estimate_500 = dp.estimate_transition_count(
            search_500,
            total_route_mi=route.total_route_mi,
            tank_range_mi=Decimal(500),
            starting_fuel=Decimal(1),
        )
        estimate_1050 = dp.estimate_transition_count(
            search_1050,
            total_route_mi=route.total_route_mi,
            tank_range_mi=Decimal(1050),
            starting_fuel=Decimal(1),
        )
        self.assertLess(
            estimate_500,
            estimate_1050,
            "the NOT TRACTABLE verdict's inversion precondition no longer "
            "holds (dallas_tx-seattle_wa@500mi's estimate is no longer "
            "smaller than @1050mi's) -- re-derive dp.py's pinned finding "
            "rather than assume it is still true.",
        )

    def test_current_policy_retains_exact_dp_on_both_floor_cells(self):
        """Renamed 2026-08-04 (plan 18.1-08) from
        ``test_current_policy_retains_the_achievable_maximum_of_the_floor_set``
        -- see this class's own docstring, point 3, for the dated
        explanation. Before this plan, ``dallas_tx-seattle_wa``@1050mi was
        asserted DEMOTED here as the NOT TRACTABLE finding's direct
        consequence; the raised, deadline-backed budget now genuinely
        admits and completes it, so both floor cells are asserted
        ``exact_dp``, not just the one that was achievable under the old
        single-threshold policy.
        """
        route_sac, candidates_sac = self._route_and_candidates(
            "sacramento_ca-salt_lake_city_ut"
        )
        plan_sac = solve(candidates_sac, route_sac.total_route_mi)
        self.assertEqual(
            plan_sac.strategy,
            SolverStrategy.EXACT_DP,
            "sacramento_ca-salt_lake_city_ut is one of the two "
            "DISPATCH_RETENTION_FLOOR cells and must always stay exact_dp "
            "-- this was true before and after the budget raise.",
        )

        route_dallas, candidates_dallas = self._route_and_candidates(
            "dallas_tx-seattle_wa"
        )
        plan_dallas_1050 = solve(
            candidates_dallas,
            route_dallas.total_route_mi,
            tank_range_mi=Decimal(1050),
        )
        self.assertEqual(
            plan_dallas_1050.strategy,
            SolverStrategy.EXACT_DP,
            "dallas_tx-seattle_wa@1050mi -- ROADMAP criterion 1's own "
            "worked example -- is now retained at exact_dp under the "
            "raised, deadline-backed dp.DP_TRANSITION_BUDGET (see "
            "dp.py's own dated 2026-08-04 note). If this ever regresses "
            "to PENALTY_AWARE_HEURISTIC, that is a real finding requiring "
            "investigation, not a silently-accepted default.",
        )


# ---------------------------------------------------------------------------
# ADMISSION_MANIFEST_VEHICLE -- D-14's match-then-represent rule. THREE
# distinct vehicles exist across this phase's evidence base and must never
# be conflated:
#
#   1. THIS manifest's corridor vehicle (below): mpg=10, starting_fuel=0.5,
#      neutral price basis -- the same vehicle CALIBRATION_CELLS above
#      already measures against (MPG, STARTING_FUEL, PENALTY at the top of
#      this module), chosen specifically so the admission figures here are
#      comparable to the 41.7% figure (non-scalar-dispatch-rule.md) they
#      replace, not a fresh, incomparable measurement.
#   2. The SPA hero preset used for the two demo-chip cells this manifest
#      does NOT cover: Semi loaded -- 6.5 mpg / 1050 mi tank / full tank.
#      That is literally what a visitor clicking the demo chip sends.
#   3. The API default used by DeployedHardwareDispatchTests above and the
#      post-deploy smoke gate: 10 mpg / 500 mi tank / full tank
#      (starting_fuel=1, NOT 0.5 -- do not confuse with #1 above; the mpg
#      figure happens to coincide, the starting fuel does not).
# ---------------------------------------------------------------------------
ADMISSION_MANIFEST_VEHICLE = {
    "mpg": MPG,
    "starting_fuel": STARTING_FUEL,
    "price_basis": "neutral",
}

# The two demo-chip slugs registered in ADMISSION_MANIFEST below use
# DEMO_CHIP_VEHICLE (not ADMISSION_MANIFEST_VEHICLE) and
# load_demo_chip_route (not load_corridor_route) -- D-14's
# match-then-represent rule, extended here (plan 18.1-08, Task 3) to the
# widened 26-cell manifest. This is the explicit per-cell vehicle
# association `DispatchAdmissionManifestTests` and
# `BudgetRaiseRegressionGuardTests` both consult.
_DEMO_CHIP_SLUGS = frozenset(chip.slug for chip in DEMO_CHIPS)

# ---------------------------------------------------------------------------
# ADMISSION_MANIFEST -- D-15: hand-pinned constants, HUMAN-EDITED ONLY.
#
# 26 cells (widened 2026-08-04, plan 18.1-08, Task 3, from the original
# 24): the full cross product of CORRIDORS (twelve slugs) and
# TANK_RANGES_MI (two pinned tank ranges) at ADMISSION_MANIFEST_VEHICLE's
# vehicle, PLUS both DEMO_CHIPS at DEMO_CHIP_VEHICLE's vehicle (D-13/D-14).
# Each value is the deterministic admission decision -- `estimate <=
# dp.DP_TRANSITION_BUDGET`, computed over the pruned search set exactly as
# solver.solve() computes it. The trailing comment on each entry carries
# that cell's measured estimate (unchanged figures, transcribed once,
# 2026-08-04).
#
# There is NO regenerate path anywhere in this repository: no command
# writes this table, and none should ever be added. A `--regenerate` flag
# would collapse "make the test pass" into a single command, which is
# precisely the failure mode criterion 1 exists to prevent (D-15). Changing
# a pinned value here is a deliberate human edit in a reviewable diff, the
# same discipline every other pinned threshold in this codebase already
# follows (DP_TRANSITION_BUDGET, DISPATCH_RETENTION_FLOOR, PENALTY_LADDER,
# ...).
#
# Cross-checked 2026-08-04 against 18.1-RESEARCH.md's own "D-03 ladder
# grounding" 24-cell table and 18.1-07-SUMMARY.md's own widened 26-cell
# sweep (measured the same session, same vehicles, same pruned-search-set
# method): every estimate below reproduces those tables' figures exactly,
# byte-for-byte -- zero discrepancies found. See 18.1-02-SUMMARY.md and
# 18.1-07-SUMMARY.md for the full side-by-side records.
#
# RE-PINNED 2026-08-04 (plan 18.1-08, Task 3) against the ADOPTED
# `dp.DP_TRANSITION_BUDGET` (50,000 -> 130,000, see dp.py's own dated
# note). The boundary marker below moved from 50,000 to 130,000
# accordingly. Exactly three booleans flip as a direct, deliberate,
# by-hand consequence of that raise -- all newly ADMITTED, none newly
# demoted (a monotone raise): `dallas_tx-seattle_wa`@500mi (61,944),
# `san_diego_ca-jacksonville_fl`@500mi (66,571), and
# `dallas_tx-seattle_wa`@1050mi (117,895 -- ROADMAP criterion 1's own
# worked example). Every other boolean is unchanged from the pre-raise
# manifest, which git history carries as the historical, superseded
# record at the old 50,000 boundary.
# ---------------------------------------------------------------------------
ADMISSION_MANIFEST = {
    ("houston_tx-chicago_il", 1050): True,  # estimate 23
    ("nashville_tn-buffalo_ny", 1050): True,  # estimate 23
    ("sacramento_ca-salt_lake_city_ut", 1050): True,  # estimate 117
    ("sacramento_ca-salt_lake_city_ut", 500): True,  # estimate 124
    ("fargo_nd-amarillo_tx", 1050): True,  # estimate 812
    ("phoenix_az-minneapolis_mn", 1050): True,  # estimate 4,809
    ("nashville_tn-buffalo_ny", 500): True,  # estimate 8,168
    ("san_diego_ca-jacksonville_fl", 1050): True,  # estimate 15,738
    ("phoenix_az-minneapolis_mn", 500): True,  # estimate 16,322
    ("miami_fl-boston_ma", 1050): True,  # estimate 19,827
    ("jacksonville_fl-bangor_me", 1050): True,  # estimate 23,013
    ("atlanta_ga-denver_co", 1050): True,  # estimate 32,487
    ("fargo_nd-amarillo_tx", 500): True,  # estimate 41,832
    ("houston_tx-chicago_il", 500): True,  # estimate 48,926
    ("dallas_tx-seattle_wa", 500): True,  # estimate 61,944 -- FLIPPED by the raise (was False); known live-breaching cell pre-hotfix, now genuinely attempted behind the deadline
    ("san_diego_ca-jacksonville_fl", 500): True,  # estimate 66,571 -- FLIPPED by the raise (was False)
    ("dallas_tx-seattle_wa", 1050): True,  # estimate 117,895 -- FLIPPED by the raise (was False); ROADMAP criterion 1's worked example
    ("demo_la_ca-denver_co-chicago_il", 1050): True,  # estimate 9,264 -- demo chip, DEMO_CHIP_VEHICLE
    # -------------------------- 130,000 boundary (dp.DP_TRANSITION_BUDGET)
    ("atlanta_ga-denver_co", 500): False,  # estimate 150,905
    ("miami_fl-boston_ma", 500): False,  # estimate 182,506
    ("demo_la_ca-new_york_ny", 1050): False,  # estimate 222,214 -- demo chip, DEMO_CHIP_VEHICLE
    ("jacksonville_fl-bangor_me", 500): False,  # estimate 356,085
    ("el_paso_tx-portland_me", 500): False,  # estimate 552,755
    ("el_paso_tx-portland_me", 1050): False,  # estimate 685,744
    ("toronto_oh-hillsboro_or", 500): False,  # estimate 1,384,311
    ("toronto_oh-hillsboro_or", 1050): False,  # estimate 2,970,562
}


def _load_manifest_cell_route_and_vehicle(slug):
    """Resolve the correct route fixture and vehicle for one
    ADMISSION_MANIFEST cell, keyed on whether `slug` is one of the two
    demo chips (D-14's match-then-represent rule, extended to the
    widened 26-cell manifest by plan 18.1-08, Task 3): demo cells use
    `load_demo_chip_route`/`DEMO_CHIP_VEHICLE`, every other cell uses
    `load_corridor_route`/`ADMISSION_MANIFEST_VEHICLE`. Both vehicles
    share `price_basis="neutral"` (PRICE_BASIS_NEUTRAL), so a single
    `factor_for` lookup built from that basis is valid for either.
    """
    if slug in _DEMO_CHIP_SLUGS:
        return load_demo_chip_route(slug), DEMO_CHIP_VEHICLE
    return load_corridor_route(slug), ADMISSION_MANIFEST_VEHICLE


class DispatchAdmissionManifestTests(RealCorridorDispatchTestCase):
    """D-12's silent-demotion-hole guard (plan 18.1-02, Task 1).

    **What is asserted, and why:** the deterministic ADMISSION decision per
    cell -- `estimate <= dp.DP_TRANSITION_BUDGET`, computed over the pruned
    search set exactly as `solver.solve()` computes it -- against the
    hand-pinned `ADMISSION_MANIFEST` above. This is a pure function of the
    committed CSV dataset, the committed route geometry fixture, and the
    `DP_TRANSITION_BUDGET` constant: reproducible on any machine, never
    flaky. A CSV change shifts candidate density, which shifts the
    estimate, which flips a cell's admission, and this test fails --
    exactly the Overture-expansion hole
    `.planning/todos/pending/non-scalar-dispatch-rule.md` names as its own
    stated minimum bar before that expansion may merge.

    **What is deliberately NOT asserted, and why:** end-to-end
    `plan.strategy` per cell. Once the DP is time-boxed (this phase's
    later plans), which arm an ADMITTED cell actually lands on stops being
    deterministic -- an admitted cell can still breach the wall-clock
    deadline on a slow box and fall back to the heuristic. A per-cell
    strategy assertion here would therefore flake by construction the
    moment time-boxing ships. Strategy is instead RECORDED (not asserted)
    by `measure_dispatch_grid` (plan 07).

    **The named, accepted limitation:** this guard does not catch
    "admitted but always breaches on this hardware" -- a cell can be
    correctly admitted here and still never actually complete live. The
    D-08 breach log line and plan 07's measurement round are what cover
    that gap; this guard only covers the admission decision itself.

    **Why `DispatchDemotionGuardTests`/`DispatchRetentionFloorGuardTests`
    above are KEPT alongside this class, not absorbed into it:** those two
    assert LIVE dispatch outcomes on the two cells with real deployed
    evidence, and their mutual anti-vacuity property (a demote-everything
    policy passes the demotion guard trivially, a retain-everything policy
    would pass the retention guard trivially, neither passes both) is a
    different property from this manifest's per-cell exactness over the
    full 26-cell grid (widened 2026-08-04, plan 18.1-08, Task 3, from the
    original 24). Both guards stay; neither replaces the other.
    """

    def test_admission_decision_matches_the_pinned_manifest(self):
        for (slug, tank_range_mi), expected_admitted in ADMISSION_MANIFEST.items():
            with self.subTest(slug=slug, tank_range_mi=tank_range_mi):
                route, vehicle = _load_manifest_cell_route_and_vehicle(slug)
                candidates = corridor.candidates(route, factor_for=self.factor_for)
                tank = Decimal(tank_range_mi)
                search_set = prune_dominated_candidates(
                    candidates,
                    tank_range_mi=tank,
                    total_route_mi=route.total_route_mi,
                )
                estimate = dp.estimate_transition_count(
                    search_set,
                    total_route_mi=route.total_route_mi,
                    tank_range_mi=tank,
                    starting_fuel=vehicle["starting_fuel"],
                )
                actual_admitted = estimate <= dp.DP_TRANSITION_BUDGET
                self.assertEqual(
                    actual_admitted,
                    expected_admitted,
                    f"{slug}@{tank_range_mi}mi: admission decision flipped -- "
                    f"pinned {expected_admitted}, but the actual estimate is "
                    f"{estimate} against dp.DP_TRANSITION_BUDGET="
                    f"{dp.DP_TRANSITION_BUDGET} (admitted={actual_admitted}). "
                    "Re-derive ADMISSION_MANIFEST deliberately (D-15: there is "
                    "no regenerate path) rather than update this number "
                    "reflexively -- a flip means the committed dataset, route "
                    "geometry, or dispatch rule genuinely changed.",
                )

    def test_manifest_covers_every_corridor_and_tank_range(self):
        expected_keys = {(c.slug, tank) for c in CORRIDORS for tank in TANK_RANGES_MI} | {
            (chip.slug, DEMO_CHIP_VEHICLE["tank_range_mi"]) for chip in DEMO_CHIPS
        }
        self.assertEqual(
            set(ADMISSION_MANIFEST.keys()),
            expected_keys,
            "ADMISSION_MANIFEST must cover exactly the full cross product "
            "of CORRIDORS x TANK_RANGES_MI plus both DEMO_CHIPS -- a "
            "corridor or demo chip added or removed later must not "
            "silently slip through uncovered.",
        )

    def test_manifest_is_not_vacuous_in_either_direction(self):
        values = set(ADMISSION_MANIFEST.values())
        self.assertIn(
            True,
            values,
            "ADMISSION_MANIFEST contains no admitted cell -- an all-False "
            "manifest is satisfiable by a degenerate demote-everything "
            "policy and would prove nothing.",
        )
        self.assertIn(
            False,
            values,
            "ADMISSION_MANIFEST contains no demoted cell -- an all-True "
            "manifest is satisfiable by a degenerate admit-everything "
            "policy and would prove nothing.",
        )


# ---------------------------------------------------------------------------
# The set of (slug, tank_range_mi) cells admitted under the PRE-RAISE
# budget (dp.DP_TRANSITION_BUDGET == 50,000, this phase's value before
# plan 18.1-08's Task 2 raised it to 130,000). Pinned by hand as its own
# historical-fact constant -- the 14 corridor cells True in
# ADMISSION_MANIFEST's pre-raise state (superseded, but on the record in
# git history) plus the one demo chip already admitted at 50,000
# (demo_la_ca-denver_co-chicago_il@1050mi, estimate 9,264;
# demo_la_ca-new_york_ny@1050mi, estimate 222,214, was NOT admitted at
# either budget). This is a recorded fact, never recomputed against the
# CURRENT budget -- recomputing it would make the comparison below
# trivially self-consistent instead of a genuine historical check.
# ---------------------------------------------------------------------------
_PRE_RAISE_BUDGET = 50_000
_PRE_RAISE_ADMITTED_CELLS = frozenset(
    {
        ("houston_tx-chicago_il", 1050),
        ("nashville_tn-buffalo_ny", 1050),
        ("sacramento_ca-salt_lake_city_ut", 1050),
        ("sacramento_ca-salt_lake_city_ut", 500),
        ("fargo_nd-amarillo_tx", 1050),
        ("phoenix_az-minneapolis_mn", 1050),
        ("nashville_tn-buffalo_ny", 500),
        ("san_diego_ca-jacksonville_fl", 1050),
        ("phoenix_az-minneapolis_mn", 500),
        ("miami_fl-boston_ma", 1050),
        ("jacksonville_fl-bangor_me", 1050),
        ("atlanta_ga-denver_co", 1050),
        ("fargo_nd-amarillo_tx", 500),
        ("houston_tx-chicago_il", 500),
        ("demo_la_ca-denver_co-chicago_il", 1050),
    }
)

# No cell in the 26-cell grid was infeasible before this phase raised the
# budget (18.1-07-SUMMARY.md's own sweep: "Zero cells censored (no
# InfeasibleRouteError)"). Pinned here, explicitly empty, rather than
# silently omitted -- if a future cell genuinely becomes infeasible for a
# reason UNRELATED to this raise (e.g. a dataset change), it belongs here
# as a named, commented exception, not folded silently into the
# assertion's own pass condition.
_KNOWN_PRE_PHASE_INFEASIBLE_CELLS = frozenset()


class BudgetRaiseRegressionGuardTests(RealCorridorDispatchTestCase):
    """Criterion 4's no-regression clause (plan 18.1-08, Task 3), as a
    PERMANENT guard rather than a one-off check run once and discarded.

    CONTEXT.md's own `<deferred>` section names the exposure this class
    proves clear directly: "the interaction between a RAISED budget and
    the currently-passing exact_dp retention set -- cells that complete
    today could newly breach once more work is admitted. Identified but
    not discussed; criterion 4's no-regression clause is where it will
    surface." Raising `dp.DP_TRANSITION_BUDGET` admits cells that were
    NEVER ATTEMPTED before under any budget; this class does not assume
    that raise is free.

    Three assertions:

    1. Every cell admitted under the historical PRE-RAISE budget
       (`_PRE_RAISE_ADMITTED_CELLS`, a recorded historical fact, not a
       recomputation) is still admitted under the adopted one. A monotone
       raise makes this trivially true today -- that is the point: it
       becomes non-trivial, and this guard starts actually firing, the
       moment a future change lowers the budget or swaps the estimator.
    2. No corridor or demo chip in the full 26-cell grid newly returns
       `infeasible_route`. Any cell already infeasible before this phase
       would be a pinned, commented exception in
       `_KNOWN_PRE_PHASE_INFEASIBLE_CELLS` -- that set is empty, so this
       assertion currently has no exemptions to hide behind.
    3. Both `dp.DISPATCH_RETENTION_FLOOR` cells still resolve to
       `exact_dp` end to end, at the same vehicle
       `DispatchRetentionFloorGuardTests` already uses -- kept as a
       second, independent confirmation of that class's own (updated)
       assertion, not a replacement for it.
    """

    def test_every_pre_raise_admitted_cell_is_still_admitted(self):
        for slug, tank_range_mi in _PRE_RAISE_ADMITTED_CELLS:
            with self.subTest(slug=slug, tank_range_mi=tank_range_mi):
                route, vehicle = _load_manifest_cell_route_and_vehicle(slug)
                candidates = corridor.candidates(route, factor_for=self.factor_for)
                tank = Decimal(tank_range_mi)
                search_set = prune_dominated_candidates(
                    candidates,
                    tank_range_mi=tank,
                    total_route_mi=route.total_route_mi,
                )
                estimate = dp.estimate_transition_count(
                    search_set,
                    total_route_mi=route.total_route_mi,
                    tank_range_mi=tank,
                    starting_fuel=vehicle["starting_fuel"],
                )
                self.assertLessEqual(
                    estimate,
                    dp.DP_TRANSITION_BUDGET,
                    f"{slug}@{tank_range_mi}mi was admitted at the "
                    f"pre-raise budget ({_PRE_RAISE_BUDGET}) but is no "
                    f"longer admitted at the adopted budget "
                    f"({dp.DP_TRANSITION_BUDGET}) -- a real regression, "
                    "not something a monotone raise should ever produce.",
                )

    def test_no_cell_newly_returns_infeasible_route(self):
        for slug, tank_range_mi in ADMISSION_MANIFEST:
            if (slug, tank_range_mi) in _KNOWN_PRE_PHASE_INFEASIBLE_CELLS:
                continue
            with self.subTest(slug=slug, tank_range_mi=tank_range_mi):
                route, vehicle = _load_manifest_cell_route_and_vehicle(slug)
                candidates = corridor.candidates(route, factor_for=self.factor_for)
                try:
                    solve(
                        candidates,
                        route.total_route_mi,
                        tank_range_mi=Decimal(tank_range_mi),
                        mpg=vehicle["mpg"],
                        starting_fuel=vehicle["starting_fuel"],
                        penalty=PENALTY,
                    )
                except InfeasibleRouteError as exc:
                    self.fail(
                        f"{slug}@{tank_range_mi}mi newly returns "
                        f"infeasible_route, not pinned as a known "
                        f"pre-phase exception: {exc}"
                    )

    def test_retention_floor_cells_still_resolve_to_exact_dp(self):
        for slug, tank_range_mi in DispatchRetentionFloorGuardTests._RETENTION_SET:
            with self.subTest(slug=slug, tank_range_mi=tank_range_mi):
                route, candidates = self._route_and_candidates(slug)
                plan = solve(
                    candidates,
                    route.total_route_mi,
                    tank_range_mi=tank_range_mi,
                )
                self.assertEqual(
                    plan.strategy,
                    SolverStrategy.EXACT_DP,
                    f"{slug}@{tank_range_mi}mi is one of the two "
                    "DISPATCH_RETENTION_FLOOR cells and must stay "
                    "exact_dp regardless of the budget raise -- a "
                    "regression here is exactly criterion 4's exposure.",
                )
