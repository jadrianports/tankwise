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
from routing.services.prune import prune_dominated_candidates
from routing.services.solver import Candidate, SolverStrategy, solve
from routing.tests.test_corridor_fixtures import (
    factor_lookup_for_basis,
    load_corridor_route,
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
    3. The CURRENT (unchanged) dispatch policy still retains ``exact_dp``
       on the achievable maximum of the floor set (exactly one:
       ``sacramento_ca-salt_lake_city_ut`` -- ``dallas_tx-seattle_wa``
       @1050mi is provably NOT retainable alongside demoting the breaching
       @500mi cell) -- so a future change that silently demotes even that
       one achievable cell is caught here.
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

    def test_current_policy_retains_the_achievable_maximum_of_the_floor_set(self):
        route_sac, candidates_sac = self._route_and_candidates(
            "sacramento_ca-salt_lake_city_ut"
        )
        plan_sac = solve(candidates_sac, route_sac.total_route_mi)
        self.assertEqual(plan_sac.strategy, SolverStrategy.EXACT_DP)

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
            SolverStrategy.PENALTY_AWARE_HEURISTIC,
            "dallas_tx-seattle_wa@1050mi is demoted under the current "
            "policy -- this is the NOT TRACTABLE finding's direct, proven "
            "consequence (dp.py's own pinned comment), not a bug. If this "
            "ever changes to EXACT_DP, dp.DISPATCH_RETENTION_FLOOR's own "
            "proof must be re-checked, not silently accepted.",
        )
