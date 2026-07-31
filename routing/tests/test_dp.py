"""Anchored unit tests for `routing.services.dp`.

Plain `django.test.SimpleTestCase` -- the DP is a pure function with no
ORM/DB access. These are the DP's unit-level anchors (one per `<behavior>`
bullet in each plan task); the property-based differentials against the
fixed-charge oracle and the frozen-greedy referee belong to plan 18-03.
"""
from decimal import Decimal

from django.test import SimpleTestCase

from routing.services.dp import (
    preflight_gap_check,
    solve_fixed_charge,
    useful_fill_levels_mi,
)
from routing.services.exceptions import InfeasibleRouteError
from routing.services.solver import Candidate, PurchaseReason


def _candidate(name, opis_id, price, distance):
    return Candidate(
        name=name,
        opis_id=opis_id,
        price_per_gallon=Decimal(price),
        distance_from_start_mi=Decimal(distance),
    )


class PreflightGapCheckTests(SimpleTestCase):
    def test_every_consecutive_gap_fits_returns_none(self):
        candidates = [
            _candidate("A", 1, "3.00", 100),
            _candidate("B", 2, "3.00", 200),
            _candidate("C", 3, "3.00", 300),
        ]
        result = preflight_gap_check(
            candidates,
            total_route_mi=Decimal(400),
            tank_range_mi=Decimal(150),
            starting_fuel=Decimal(1),
        )
        self.assertIsNone(result)

    def test_start_asymmetry_raises_with_fuel_on_board_not_tank_capacity(self):
        candidates = [_candidate("Only Stop", 1, "3.00", 400)]
        with self.assertRaises(InfeasibleRouteError) as ctx:
            preflight_gap_check(
                candidates,
                total_route_mi=Decimal(500),
                tank_range_mi=Decimal(500),
                starting_fuel=Decimal("0.5"),
            )
        exc = ctx.exception
        self.assertEqual(exc.from_station, "START")
        self.assertEqual(exc.to_station, "Only Stop")
        self.assertEqual(exc.gap_mi, Decimal(400))
        self.assertEqual(exc.max_range_mi, Decimal(250))
        self.assertIsNone(exc.leg_index)
        self.assertIsNone(exc.leg_coords)

    def test_no_candidates_and_route_exceeds_starting_range_raises_start_to_finish(
        self,
    ):
        with self.assertRaises(InfeasibleRouteError) as ctx:
            preflight_gap_check(
                [],
                total_route_mi=Decimal(600),
                tank_range_mi=Decimal(500),
                starting_fuel=Decimal(1),
            )
        exc = ctx.exception
        self.assertEqual(exc.from_station, "START")
        self.assertEqual(exc.to_station, "FINISH")

    def test_unreachable_trailing_gap_raises_from_last_station_at_full_capacity(self):
        candidates = [
            _candidate("A", 1, "3.00", 50),
            _candidate("B", 2, "3.00", 100),
        ]
        with self.assertRaises(InfeasibleRouteError) as ctx:
            preflight_gap_check(
                candidates,
                total_route_mi=Decimal(400),
                tank_range_mi=Decimal(150),
                starting_fuel=Decimal(1),
            )
        exc = ctx.exception
        self.assertEqual(exc.from_station, "B")
        self.assertEqual(exc.to_station, "FINISH")
        self.assertEqual(exc.max_range_mi, Decimal(150))

    def test_never_sets_leg_index_or_leg_coords(self):
        with self.assertRaises(InfeasibleRouteError) as ctx:
            preflight_gap_check(
                [],
                total_route_mi=Decimal(1000),
                tank_range_mi=Decimal(500),
                starting_fuel=Decimal(1),
            )
        exc = ctx.exception
        self.assertIsNone(exc.leg_index)
        self.assertIsNone(exc.leg_coords)


class UsefulFillLevelsMiTests(SimpleTestCase):
    def test_returns_strictly_increasing_deduplicated_bounded_tuple(self):
        # position 0, arrived with 0 fuel, tank 150; three nodes ahead but
        # only the first (100) is within tank range.
        levels = useful_fill_levels_mi(
            Decimal(0),
            Decimal(0),
            tank_range_mi=Decimal(150),
            nodes_ahead_mi=[Decimal(100), Decimal(200), Decimal(300)],
        )
        self.assertEqual(levels, tuple(sorted(set(levels))))
        self.assertEqual(list(levels), sorted(levels))
        # Bound: at most (candidates strictly ahead) + 1 distinct levels.
        self.assertLessEqual(len(levels), 3 + 1)
        # Always includes the level that exactly reaches the nearest node
        # ahead (100) and the level that fills to tank_range_mi (150).
        self.assertIn(Decimal(100), levels)
        self.assertIn(Decimal(150), levels)

    def test_arrival_level_and_cap_included_clamped(self):
        levels = useful_fill_levels_mi(
            Decimal(50),
            Decimal(20),
            tank_range_mi=Decimal(150),
            nodes_ahead_mi=[Decimal(150), Decimal(600)],
        )
        # Arrival level (buy nothing) present.
        self.assertIn(Decimal(20), levels)
        # Exact reach of the node at 150 (100 mi away) present.
        self.assertIn(Decimal(100), levels)
        # Node at 600 is out of tank range (550 mi away > 150) -- excluded.
        self.assertNotIn(Decimal(550), levels)
        # Fill to capacity present.
        self.assertIn(Decimal(150), levels)
        # Nothing below the arrival level.
        self.assertTrue(all(level >= Decimal(20) for level in levels))


class SolveFixedChargeTests(SimpleTestCase):
    def test_single_reachable_candidate_costs_nothing_when_route_fits_on_starting_tank(
        self,
    ):
        candidates = [_candidate("Cheap", 1, "2.00", 200)]
        plan = solve_fixed_charge(
            candidates,
            total_route_mi=Decimal(300),
            tank_range_mi=Decimal(500),
            mpg=Decimal(10),
            starting_fuel=Decimal(1),
            penalty=Decimal(0),
        )
        self.assertEqual(plan.stops, [])
        self.assertEqual(plan.total_cost, Decimal(0))

    def test_reaches_cheaper_stop_buying_only_enough_to_get_there(self):
        candidates = [
            _candidate("A", 1, "4.00", 100),
            _candidate("B", 2, "3.00", 300),
        ]
        plan = solve_fixed_charge(
            candidates,
            total_route_mi=Decimal(600),
            tank_range_mi=Decimal(400),
            mpg=Decimal(10),
            starting_fuel=Decimal("0.25"),
            penalty=Decimal(0),
        )
        self.assertEqual([s.opis_id for s in plan.stops], [1, 2])
        self.assertEqual(plan.stops[0].purchase_reason, PurchaseReason.REACH_CHEAPER_STOP)
        # Bought only enough to reach B (200 mi), not a full tank (400 mi).
        self.assertEqual(plan.stops[0].gallons, Decimal("20.00"))

    def test_last_stop_buys_exactly_enough_to_finish_never_a_full_tank(self):
        candidates = [
            _candidate("A", 1, "4.00", 100),
            _candidate("B", 2, "3.00", 300),
        ]
        plan = solve_fixed_charge(
            candidates,
            total_route_mi=Decimal(600),
            tank_range_mi=Decimal(400),
            mpg=Decimal(10),
            starting_fuel=Decimal("0.25"),
            penalty=Decimal(0),
        )
        last_stop = plan.stops[-1]
        self.assertEqual(last_stop.purchase_reason, PurchaseReason.REACH_FINISH)
        # 300 mi remaining to FINISH (600 - 300), not a full 400 mi tank.
        self.assertEqual(last_stop.gallons, Decimal("30"))

    def test_bypass_cheaper_not_worth_stop_at_high_penalty(self):
        candidates = [
            _candidate("A", 1, "3.50", 250),
            _candidate("B", 2, "3.00", 500),
            _candidate("C", 3, "3.55", 700),
        ]
        params = dict(
            total_route_mi=Decimal(1050),
            tank_range_mi=Decimal(500),
            mpg=Decimal(10),
            starting_fuel=Decimal("0.5"),
        )

        cheap_plan = solve_fixed_charge(candidates, penalty=Decimal(0), **params)
        self.assertIn(2, [s.opis_id for s in cheap_plan.stops])

        expensive_plan = solve_fixed_charge(candidates, penalty=Decimal(35), **params)
        self.assertNotIn(2, [s.opis_id for s in expensive_plan.stops])
        first_stop = expensive_plan.stops[0]
        self.assertEqual(
            first_stop.purchase_reason, PurchaseReason.BYPASS_CHEAPER_NOT_WORTH_STOP
        )
        self.assertGreaterEqual(first_stop.bypassed_cheaper_count, 1)
        self.assertGreater(first_stop.bypassed_saving_forgone, Decimal(0))

    def test_co_located_candidates_are_both_visitable_cheaper_one_wins(self):
        candidates = [
            _candidate("Pricier", 1, "4.00", 50),
            _candidate("Cheaper", 2, "3.00", 50),
        ]
        plan = solve_fixed_charge(
            candidates,
            total_route_mi=Decimal(100),
            tank_range_mi=Decimal(200),
            mpg=Decimal(10),
            starting_fuel=Decimal("0.25"),
            penalty=Decimal(0),
        )
        # Neither candidate silently collapsed: the strictly cheaper
        # co-located twin is the one actually visited.
        self.assertEqual([s.opis_id for s in plan.stops], [2])

    def test_penalised_objective_equals_total_cost_plus_penalty_times_stops(self):
        candidates = [
            _candidate("A", 1, "3.50", 250),
            _candidate("B", 2, "3.00", 500),
            _candidate("C", 3, "3.55", 700),
        ]
        for penalty in (Decimal(0), Decimal(10), Decimal(35)):
            plan = solve_fixed_charge(
                candidates,
                total_route_mi=Decimal(1050),
                tank_range_mi=Decimal(500),
                mpg=Decimal(10),
                starting_fuel=Decimal("0.5"),
                penalty=penalty,
            )
            self.assertEqual(
                plan.penalised_objective,
                plan.total_cost + plan.penalty_applied * len(plan.stops),
            )
            self.assertEqual(plan.penalty_applied, penalty)
            if penalty == Decimal(0):
                self.assertEqual(plan.penalised_objective, plan.total_cost)

    def test_deterministic_across_ten_repeat_solves(self):
        candidates = [
            _candidate(chr(65 + i), i, f"3.{i:02d}", 100 * (i + 1)) for i in range(6)
        ]
        params = dict(
            total_route_mi=Decimal(900),
            tank_range_mi=Decimal(400),
            mpg=Decimal(10),
            starting_fuel=Decimal("0.5"),
            penalty=Decimal(35),
        )

        def _key(plan):
            return [
                (s.opis_id, s.gallons, s.cost, s.purchase_reason) for s in plan.stops
            ]

        baseline = _key(solve_fixed_charge(candidates, **params))
        for _ in range(10):
            self.assertEqual(_key(solve_fixed_charge(candidates, **params)), baseline)


class ReasonReconstructionRegressionTests(SimpleTestCase):
    """Anchors two rationale-reconstruction defects found by the
    orchestrator's independent verification of plan 18-03, both at
    `penalty=0` on the unpruned arm -- fixing a fabricated
    `BYPASS_CHEAPER_NOT_WORTH_STOP` reason where the DP's own reconstructed
    plan ALSO stopped at the station it claimed to have bypassed (Defect
    A), and settling a genuine `reach_finish` vs. `reach_cheaper_stop`
    reason-label disagreement against the frozen greedy on the merits
    (Defect B), rather than relaxing either assertion to reach green."""

    def test_full_fill_landing_exactly_on_the_bypassed_station_is_not_a_bypass(self):
        # Defect A. S1 (pricier, mile 1) full-fills to a level that
        # coincides exactly with S0 (cheaper, mile 479) -- the tank range
        # (478 mi) is exactly S0's distance from S1. The DP used to label
        # this purchase BYPASS_CHEAPER_NOT_WORTH_STOP targeting S0 while
        # its own reconstructed plan ALSO stopped at S0 immediately after
        # -- nothing was bypassed. At penalty=0 this reason is also
        # structurally impossible (no penalty exists to outweigh a
        # saving), which is a second, independent reason the old label
        # was wrong here.
        candidates = [
            _candidate("S0", 0, "1.00", 479),
            _candidate("S1", 1, "1.01", 1),
        ]
        plan = solve_fixed_charge(
            candidates,
            total_route_mi=Decimal(480),
            tank_range_mi=Decimal(478),
            mpg=Decimal(1),
            starting_fuel=Decimal("0.01"),
            penalty=Decimal(0),
        )
        self.assertEqual([s.opis_id for s in plan.stops], [1, 0])
        s1_stop = plan.stops[0]
        self.assertEqual(s1_stop.purchase_reason, PurchaseReason.REACH_CHEAPER_STOP)
        self.assertEqual(s1_stop.reason_target_opis_id, 0)
        self.assertEqual(s1_stop.bypassed_cheaper_count, 0)
        self.assertIsNone(s1_stop.bypassed_saving_forgone)
        self.assertEqual(plan.total_cost, Decimal("479.9622"))
        # The D-09 invariant this defect violated: the fifth reason must
        # never appear at penalty=0, on any stop.
        for stop in plan.stops:
            self.assertNotEqual(
                stop.purchase_reason, PurchaseReason.BYPASS_CHEAPER_NOT_WORTH_STOP
            )

    def test_finish_coincident_cheaper_station_is_labelled_reach_finish(self):
        # Defect B. S0 sits exactly at the route's finish (mile 2 of a
        # 2-mile route) and is cheaper than S1. Buying at S1 exactly
        # enough to complete the trip lands, coincidentally, on S0's own
        # position too -- but the reconstructed plan makes no purchase at
        # S0 (buy_mi=0 there) and the journey ends. REACH_FINISH is the
        # correct label: the purchase's purpose is completing the trip,
        # not routing toward a station the plan never actually visits for
        # a purchase. See `routing/tests/test_dp_differential.py`'s
        # 2026-08-01 docstring amendment for the full merits discussion
        # (why the frozen greedy's `reach_cheaper_stop` label is the
        # walk-algorithm artifact here, not the DP's `reach_finish`).
        candidates = [
            _candidate("S0", 0, "1.00", 2),
            _candidate("S1", 1, "1.01", 1),
        ]
        plan = solve_fixed_charge(
            candidates,
            total_route_mi=Decimal(2),
            tank_range_mi=Decimal(20),
            mpg=Decimal(1),
            starting_fuel=Decimal("0.05"),
            penalty=Decimal(0),
        )
        self.assertEqual([s.opis_id for s in plan.stops], [1])
        only_stop = plan.stops[0]
        self.assertEqual(only_stop.purchase_reason, PurchaseReason.REACH_FINISH)
        self.assertEqual(only_stop.gallons, Decimal("1"))
        self.assertEqual(only_stop.cost, Decimal("1.01"))
