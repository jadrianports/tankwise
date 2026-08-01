"""Differential and unit tests for `routing.services.greedy` (Phase
18-04c): the production DP-fallback greedy, restored independently of
`routing/tests/frozen_greedy.py`'s test-only differential referee.

The central claim under test: `greedy.solve_greedy` and
`frozen_greedy.solve` implement the identical pre-Phase-18 algorithm, so
they must agree on every feasible input -- stops, gallons, cost, and
every `purchase_reason` -- regardless of `penalty` (which `solve_greedy`
never uses to make a routing decision, only to compute the reported
`penalised_objective`/`penalty_applied`).
"""
from decimal import Decimal

from django.test import SimpleTestCase
from hypothesis import given, settings

from routing.services import greedy
from routing.services.exceptions import InfeasibleRouteError
from routing.services.solver import Candidate, PurchaseReason
from routing.tests import frozen_greedy
from routing.tests.test_solver_fixed_charge_optimality import single_leg_routes


class GreedyMatchesFrozenReferenceTests(SimpleTestCase):
    """`solve_greedy` must be plan-identical to `frozen_greedy.solve` on
    every input, at every `penalty` (the algorithm is structurally
    penalty-blind, so the routing decision must never move when only
    `penalty` changes)."""

    @given(single_leg_routes())
    @settings(deadline=None, max_examples=100, derandomize=True)
    def test_solve_greedy_matches_frozen_greedy_at_every_penalty(self, drawn_route):
        candidates, total_route_mi, tank_range_mi, mpg, starting_fuel = drawn_route

        try:
            reference_plan = frozen_greedy.solve(
                candidates,
                total_route_mi,
                tank_range_mi=tank_range_mi,
                mpg=mpg,
                starting_fuel=starting_fuel,
            )
            reference_feasible = True
        except InfeasibleRouteError:
            reference_plan = None
            reference_feasible = False

        for penalty in (Decimal(0), Decimal(10), Decimal(35)):
            with self.subTest(penalty=penalty):
                try:
                    plan = greedy.solve_greedy(
                        candidates,
                        total_route_mi,
                        tank_range_mi=tank_range_mi,
                        mpg=mpg,
                        starting_fuel=starting_fuel,
                        penalty=penalty,
                    )
                    feasible = True
                except InfeasibleRouteError:
                    plan = None
                    feasible = False

                self.assertEqual(
                    feasible,
                    reference_feasible,
                    "solve_greedy and frozen_greedy.solve disagreed on "
                    "feasibility for the same input",
                )
                if not feasible:
                    continue

                self.assertEqual(len(plan.stops), len(reference_plan.stops))
                for actual, expected in zip(plan.stops, reference_plan.stops):
                    self.assertEqual(actual.opis_id, expected.opis_id)
                    self.assertEqual(actual.name, expected.name)
                    self.assertEqual(
                        actual.distance_from_start_mi,
                        expected.distance_from_start_mi,
                    )
                    self.assertEqual(actual.gallons, expected.gallons)
                    self.assertEqual(actual.cost, expected.cost)
                    self.assertEqual(actual.purchase_reason, expected.purchase_reason)
                    self.assertEqual(
                        actual.reason_target_opis_id, expected.reason_target_opis_id
                    )
                    self.assertEqual(
                        actual.reason_target_name, expected.reason_target_name
                    )

                self.assertEqual(plan.total_cost, reference_plan.total_cost)
                self.assertEqual(plan.total_gallons, reference_plan.total_gallons)

                # `penalty` never influences the routing decision above --
                # only these two reported fields.
                self.assertEqual(plan.penalty_applied, penalty)
                self.assertEqual(
                    plan.penalised_objective,
                    plan.total_cost + penalty * len(plan.stops),
                )


class GreedyNeverEmitsBypassReasonTests(SimpleTestCase):
    """`solve_greedy` never reasons about a stop's fixed cost at all, so
    it can never emit the fifth, DP-only `purchase_reason` value."""

    @given(single_leg_routes())
    @settings(deadline=None, max_examples=100, derandomize=True)
    def test_no_stop_carries_the_bypass_reason(self, drawn_route):
        candidates, total_route_mi, tank_range_mi, mpg, starting_fuel = drawn_route
        try:
            plan = greedy.solve_greedy(
                candidates,
                total_route_mi,
                tank_range_mi=tank_range_mi,
                mpg=mpg,
                starting_fuel=starting_fuel,
                penalty=Decimal(35),
            )
        except InfeasibleRouteError:
            return
        for stop in plan.stops:
            self.assertNotEqual(
                stop.purchase_reason, PurchaseReason.BYPASS_CHEAPER_NOT_WORTH_STOP
            )


class GreedyRawPlanFieldContractTests(SimpleTestCase):
    """`solve_greedy`'s returned `FuelStop`s leave the D-20 reporting
    fields (`skipped_count`/`skipped_avg_price`/`price_percentile`/
    `corridor_avg_price`) at their dataclass defaults -- `solver.solve()`
    rebuilds those over the full candidate list itself, exactly as it
    already does for `dp.solve_fixed_charge`'s own raw output."""

    def test_raw_stops_leave_reporting_fields_at_defaults(self):
        candidates = [
            Candidate(
                name="A",
                opis_id=1,
                price_per_gallon=Decimal("3.00"),
                distance_from_start_mi=Decimal(100),
            ),
            Candidate(
                name="B",
                opis_id=2,
                price_per_gallon=Decimal("2.00"),
                distance_from_start_mi=Decimal(450),
            ),
        ]
        plan = greedy.solve_greedy(
            candidates,
            Decimal(900),
            tank_range_mi=Decimal(500),
            mpg=Decimal(10),
            starting_fuel=Decimal(1),
            penalty=Decimal(35),
        )
        self.assertGreater(len(plan.stops), 0)
        for stop in plan.stops:
            self.assertEqual(stop.skipped_count, 0)
            self.assertIsNone(stop.skipped_avg_price)
            self.assertIsNone(stop.price_percentile)
            self.assertIsNone(stop.corridor_avg_price)
            self.assertEqual(stop.bypassed_cheaper_count, 0)
            self.assertIsNone(stop.bypassed_saving_forgone)

    def test_default_penalty_is_zero_and_reported_accordingly(self):
        candidates = [
            Candidate(
                name="A",
                opis_id=1,
                price_per_gallon=Decimal("3.00"),
                distance_from_start_mi=Decimal(400),
            ),
        ]
        plan = greedy.solve_greedy(
            candidates,
            Decimal(900),
            tank_range_mi=Decimal(500),
            mpg=Decimal(10),
            starting_fuel=Decimal(1),
        )
        self.assertGreater(len(plan.stops), 0)
        self.assertEqual(plan.penalty_applied, Decimal(0))
        self.assertEqual(plan.penalised_objective, plan.total_cost)
