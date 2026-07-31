"""Anchored unit tests for `routing.services.dp`.

Plain `django.test.SimpleTestCase` -- the DP is a pure function with no
ORM/DB access. These are the DP's unit-level anchors (one per `<behavior>`
bullet in each plan task); the property-based differentials against the
fixed-charge oracle and the frozen-greedy referee belong to plan 18-03.
"""
from decimal import Decimal

from django.test import SimpleTestCase

from routing.services.dp import preflight_gap_check, useful_fill_levels_mi
from routing.services.exceptions import InfeasibleRouteError
from routing.services.solver import Candidate


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
