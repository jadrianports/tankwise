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
    greedy path, a light one takes the DP path."""

    def test_heavy_corridor_takes_the_greedy_fallback(self):
        plan = self._solve("toronto_oh-hillsboro_or", Decimal(1050))
        self.assertEqual(plan.strategy, SolverStrategy.GREEDY_FALLBACK)
        self.assertGreater(len(plan.stops), 0)

    def test_light_corridor_takes_the_exact_dp(self):
        plan = self._solve("dallas_tx-seattle_wa", Decimal(1050))
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
