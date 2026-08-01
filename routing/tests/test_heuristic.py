"""Tests for `routing.services.heuristic.solve_penalty_aware_heuristic`
(Phase 18-04d): feasibility, determinism, and the fewer-stops property on
the real heavy corridors that motivated it.

These are property tests, not exact-count pins (per this plan's own
instruction): the heuristic is a single-pass approximation, not the exact
DP, so its precise stop count/cost is expected to drift as the algorithm
is refined -- what must never change is that it stays feasible,
deterministic, and materially better than the fixed-charge-blind greedy
it replaced at the dispatch seam.
"""
import io
from decimal import Decimal

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from hypothesis import given, settings

from routing.services import corridor, dp, greedy, heuristic
from routing.services.dp import preflight_gap_check
from routing.services.exceptions import InfeasibleRouteError
from routing.services.prune import prune_dominated_candidates
from routing.tests.test_corridor_fixtures import (
    factor_lookup_for_basis,
    load_corridor_route,
)
from routing.tests.test_solver_fixed_charge_optimality import single_leg_routes

MPG = Decimal(10)
STARTING_FUEL = Decimal("0.5")
PENALTY = Decimal(35)

# Tolerance for the replay check below -- Decimal division at the default
# 28-digit context is inexact, so exact equality on an accumulated fuel
# level is not the right assertion. Far tighter than any real-world
# gallon/mile quantity this codebase ever produces.
_REPLAY_TOLERANCE = Decimal("0.0000001")


def _replay_is_feasible(plan, *, starting_fuel, tank_range_mi, mpg, total_route_mi):
    """Walk `plan.stops` in order, tracking fuel on board exactly the way
    a real vehicle would, and confirm the tank never goes negative before
    a purchase and never exceeds `tank_range_mi` after one. Returns
    `(ok, reason)` rather than asserting directly, so callers can attach
    the reason to a Hypothesis/subTest failure message."""
    pos = Decimal(0)
    fuel = starting_fuel * tank_range_mi
    for stop in plan.stops:
        gap = stop.distance_from_start_mi - pos
        if gap > fuel + _REPLAY_TOLERANCE:
            return False, f"ran dry before stop {stop.opis_id!r}: gap={gap} fuel={fuel}"
        fuel -= gap
        fuel += stop.gallons * mpg
        if fuel > tank_range_mi + _REPLAY_TOLERANCE:
            return False, f"overfilled at stop {stop.opis_id!r}: fuel={fuel} > tank={tank_range_mi}"
        if fuel < -_REPLAY_TOLERANCE:
            return False, f"negative fuel at stop {stop.opis_id!r}: fuel={fuel}"
        pos = stop.distance_from_start_mi
    gap = total_route_mi - pos
    if gap > fuel + _REPLAY_TOLERANCE:
        return False, f"ran dry before FINISH: gap={gap} fuel={fuel}"
    return True, "ok"


class FeasibilityPropertyTests(SimpleTestCase):
    """SOLV-05-style guarantee for the heuristic: it must never claim a
    genuinely infeasible route is feasible, and every plan it DOES return
    must survive the fuel replay above -- the tank is never asked to
    travel farther than it holds, and never receives more than its own
    capacity. `dp.preflight_gap_check` is reused as the feasibility
    oracle (it is the codebase's own single source of truth for whether a
    route is feasible at all -- see its own docstring), never
    re-implemented here.
    """

    @given(single_leg_routes())
    @settings(deadline=None, max_examples=200, derandomize=True)
    def test_heuristic_never_runs_dry_and_never_turns_feasible_into_infeasible(self, drawn_route):
        candidates, total_route_mi, tank_range_mi, mpg, starting_fuel = drawn_route

        try:
            preflight_gap_check(
                candidates,
                total_route_mi=total_route_mi,
                tank_range_mi=tank_range_mi,
                starting_fuel=starting_fuel,
            )
            oracle_feasible = True
        except InfeasibleRouteError:
            oracle_feasible = False

        for penalty in (Decimal(0), Decimal(10), Decimal(35)):
            with self.subTest(penalty=penalty):
                try:
                    plan = heuristic.solve_penalty_aware_heuristic(
                        candidates,
                        total_route_mi,
                        tank_range_mi=tank_range_mi,
                        mpg=mpg,
                        starting_fuel=starting_fuel,
                        penalty=penalty,
                    )
                    heuristic_feasible = True
                except InfeasibleRouteError:
                    heuristic_feasible = False
                    plan = None

                if oracle_feasible:
                    self.assertTrue(
                        heuristic_feasible,
                        "the oracle found this route feasible but the heuristic "
                        "raised InfeasibleRouteError -- a feasible route must "
                        "never be turned infeasible",
                    )
                    ok, reason = _replay_is_feasible(
                        plan,
                        starting_fuel=starting_fuel,
                        tank_range_mi=tank_range_mi,
                        mpg=mpg,
                        total_route_mi=total_route_mi,
                    )
                    self.assertTrue(ok, reason)
                else:
                    # The oracle itself found no feasible plan exists --
                    # the heuristic raising is the ONLY correct outcome
                    # here (it must never fabricate a plan for a
                    # genuinely infeasible route).
                    self.assertFalse(
                        heuristic_feasible,
                        "the oracle found this route infeasible but the "
                        "heuristic returned a plan anyway",
                    )


class DeterminismPropertyTests(SimpleTestCase):
    """Same input -> same plan, always -- load-bearing for the response
    cache exactly as `solver.solve()`'s own dispatch determinism is (see
    `routing/cache.py`'s `build_cache_key` docstring): a live request must
    never choose a different heuristic plan on a cache miss than it would
    have on a cache hit."""

    @given(single_leg_routes())
    @settings(deadline=None, max_examples=100, derandomize=True)
    def test_repeat_calls_on_identical_input_produce_an_identical_plan(self, drawn_route):
        candidates, total_route_mi, tank_range_mi, mpg, starting_fuel = drawn_route
        kwargs = dict(
            tank_range_mi=tank_range_mi,
            mpg=mpg,
            starting_fuel=starting_fuel,
            penalty=Decimal(35),
        )

        try:
            plan_a = heuristic.solve_penalty_aware_heuristic(candidates, total_route_mi, **kwargs)
        except InfeasibleRouteError:
            return  # nothing to compare; feasibility itself is covered above

        plan_b = heuristic.solve_penalty_aware_heuristic(candidates, total_route_mi, **kwargs)

        self.assertEqual(plan_a.total_cost, plan_b.total_cost)
        self.assertEqual(plan_a.total_gallons, plan_b.total_gallons)
        self.assertEqual(
            [(s.opis_id, s.gallons, s.cost, s.purchase_reason) for s in plan_a.stops],
            [(s.opis_id, s.gallons, s.cost, s.purchase_reason) for s in plan_b.stops],
        )

    @given(single_leg_routes())
    @settings(deadline=None, max_examples=100, derandomize=True)
    def test_is_independent_of_input_list_order(self, drawn_route):
        candidates, total_route_mi, tank_range_mi, mpg, starting_fuel = drawn_route
        kwargs = dict(
            tank_range_mi=tank_range_mi,
            mpg=mpg,
            starting_fuel=starting_fuel,
            penalty=Decimal(35),
        )

        try:
            forward = heuristic.solve_penalty_aware_heuristic(candidates, total_route_mi, **kwargs)
        except InfeasibleRouteError:
            return  # nothing to compare; feasibility itself is covered above

        reversed_order = heuristic.solve_penalty_aware_heuristic(
            list(reversed(candidates)), total_route_mi, **kwargs
        )

        self.assertEqual(forward.total_cost, reversed_order.total_cost)
        self.assertEqual(
            [(s.opis_id, s.gallons, s.purchase_reason) for s in forward.stops],
            [(s.opis_id, s.gallons, s.purchase_reason) for s in reversed_order.stops],
        )


class FewerStopsThanGreedyOnHeavyCorridorsTests(TestCase):
    """Design requirement 5's replacement: on the real corridors dense
    enough that `solver.solve()` dispatches away from the exact DP, the
    penalty-aware heuristic must produce MATERIALLY fewer stops than the
    fixed-charge-blind greedy it replaced -- not a pinned exact count
    (the algorithm may still be refined), just the property that
    motivated swapping the fallback in the first place."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_stations", stdout=io.StringIO())
        corridor.warm_index()

    def setUp(self):
        self.factor_for = factor_lookup_for_basis("neutral")

    # The five real corridor/tank-range cells 18-04c-SUMMARY.md's own
    # table names as dispatching to the fallback (`estimate_transition_count`
    # over `dp.DP_TRANSITION_BUDGET`) -- exactly the cells this plan's own
    # `<why>` table reports "stops NOW" for.
    HEAVY_CELLS = (
        ("toronto_oh-hillsboro_or", Decimal(1050)),
        ("toronto_oh-hillsboro_or", Decimal(500)),
        ("el_paso_tx-portland_me", Decimal(500)),
        ("el_paso_tx-portland_me", Decimal(1050)),
        ("jacksonville_fl-bangor_me", Decimal(500)),
    )

    def test_heuristic_takes_materially_fewer_stops_than_the_naive_greedy(self):
        for slug, tank_range_mi in self.HEAVY_CELLS:
            with self.subTest(slug=slug, tank_range_mi=tank_range_mi):
                route = load_corridor_route(slug)
                candidates = corridor.candidates(route, factor_for=self.factor_for)

                greedy_plan = greedy.solve_greedy(
                    candidates,
                    route.total_route_mi,
                    tank_range_mi=tank_range_mi,
                    mpg=MPG,
                    starting_fuel=STARTING_FUEL,
                    penalty=PENALTY,
                )
                heuristic_plan = heuristic.solve_penalty_aware_heuristic(
                    candidates,
                    route.total_route_mi,
                    tank_range_mi=tank_range_mi,
                    mpg=MPG,
                    starting_fuel=STARTING_FUEL,
                    penalty=PENALTY,
                )

                # "Materially fewer" -- strictly less, and by more than a
                # single stop, so this cannot pass on a one-off tie-break
                # difference that carries no real signal.
                self.assertLess(len(heuristic_plan.stops), len(greedy_plan.stops))
                self.assertLessEqual(
                    len(heuristic_plan.stops), len(greedy_plan.stops) - 2,
                    f"{slug}@{tank_range_mi}: heuristic took "
                    f"{len(heuristic_plan.stops)} stops, greedy took "
                    f"{len(greedy_plan.stops)} -- expected a material "
                    "reduction, not a marginal one",
                )

    def test_heuristic_plan_is_feasible_on_every_heavy_cell(self):
        for slug, tank_range_mi in self.HEAVY_CELLS:
            with self.subTest(slug=slug, tank_range_mi=tank_range_mi):
                route = load_corridor_route(slug)
                candidates = corridor.candidates(route, factor_for=self.factor_for)
                plan = heuristic.solve_penalty_aware_heuristic(
                    candidates,
                    route.total_route_mi,
                    tank_range_mi=tank_range_mi,
                    mpg=MPG,
                    starting_fuel=STARTING_FUEL,
                    penalty=PENALTY,
                )
                ok, reason = _replay_is_feasible(
                    plan,
                    starting_fuel=STARTING_FUEL,
                    tank_range_mi=tank_range_mi,
                    mpg=MPG,
                    total_route_mi=route.total_route_mi,
                )
                self.assertTrue(ok, f"{slug}@{tank_range_mi}: {reason}")


class QualityGapAgainstTheExactDPTests(TestCase):
    """The quantified deliverable from this plan's own `<why>` (task 3):
    on the real corridor/tank-range cells where the exact DP itself is
    tractable, compare it directly against the heuristic on the SAME
    input treatment `solver.solve()`'s own dispatch would give each
    algorithm (the DP over the PRUNED search set, the heuristic over the
    FULL unpruned candidate list -- see `solver.solve()`'s own
    docstring). `18-04d-SUMMARY.md` reports the exact measured numbers;
    this test is a loose regression guard on that gap, not a pin of the
    current figures -- a generous multiplicative ceiling (comfortably
    above every measured cell) that only fails if a future change to
    either algorithm blows the approximation gap open, not on ordinary
    algorithmic refinement."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_stations", stdout=io.StringIO())
        corridor.warm_index()

    def setUp(self):
        self.factor_for = factor_lookup_for_basis("neutral")

    # The seven corridor/tank-range cells 18-04c-SUMMARY.md's own
    # "Before/After" table marks `exact_dp` -- the DP-tractable subset
    # this task's own instructions name.
    TRACTABLE_CELLS = (
        ("miami_fl-boston_ma", Decimal(500)),
        ("atlanta_ga-denver_co", Decimal(500)),
        ("dallas_tx-seattle_wa", Decimal(1050)),
        ("dallas_tx-seattle_wa", Decimal(500)),
        ("san_diego_ca-jacksonville_fl", Decimal(500)),
        ("phoenix_az-minneapolis_mn", Decimal(1050)),
        ("houston_tx-chicago_il", Decimal(1050)),
    )

    # Measured max penalized-objective gap across the seven cells is
    # ~12.5% (18-04d-SUMMARY.md); 2.0x is a deliberately loose ceiling
    # (an order of magnitude of headroom) so this test catches a genuine
    # regression, not ordinary measurement noise or a deliberate future
    # tuning change.
    _MAX_PENALIZED_OBJECTIVE_RATIO = Decimal("2.0")

    def test_heuristic_stays_within_a_loose_bound_of_the_dp_on_every_tractable_cell(self):
        for slug, tank_range_mi in self.TRACTABLE_CELLS:
            with self.subTest(slug=slug, tank_range_mi=tank_range_mi):
                route = load_corridor_route(slug)
                candidates = corridor.candidates(route, factor_for=self.factor_for)
                search_set = prune_dominated_candidates(
                    candidates,
                    tank_range_mi=tank_range_mi,
                    total_route_mi=route.total_route_mi,
                )

                dp_plan = dp.solve_fixed_charge(
                    search_set,
                    total_route_mi=route.total_route_mi,
                    tank_range_mi=tank_range_mi,
                    mpg=MPG,
                    starting_fuel=STARTING_FUEL,
                    penalty=PENALTY,
                )
                heuristic_plan = heuristic.solve_penalty_aware_heuristic(
                    candidates,
                    route.total_route_mi,
                    tank_range_mi=tank_range_mi,
                    mpg=MPG,
                    starting_fuel=STARTING_FUEL,
                    penalty=PENALTY,
                )

                self.assertLessEqual(
                    heuristic_plan.penalised_objective,
                    dp_plan.penalised_objective * self._MAX_PENALIZED_OBJECTIVE_RATIO,
                    f"{slug}@{tank_range_mi}: heuristic penalized objective "
                    f"{heuristic_plan.penalised_objective} exceeds "
                    f"{self._MAX_PENALIZED_OBJECTIVE_RATIO}x the DP's "
                    f"{dp_plan.penalised_objective}",
                )
