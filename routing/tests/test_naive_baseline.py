from decimal import Decimal

from django.test import SimpleTestCase

from routing.services import Candidate, FuelPlan, naive_baseline, solve
from routing.services.exceptions import InfeasibleRouteError, InvalidRouteInputError
from routing.services.naive_baseline import Savings, compute_savings


def make_candidate(price, distance, *, name="STOP", opis_id=1):
    return Candidate(
        name=name,
        opis_id=opis_id,
        price_per_gallon=Decimal(str(price)),
        distance_from_start_mi=Decimal(str(distance)),
    )


class FarthestNotCheapestTests(SimpleTestCase):
    """The baseline never consults price when choosing where to stop --
    only the farthest reachable candidate is chosen, never the cheapest."""

    def test_stops_at_farthest_reachable_not_cheapest(self):
        candidates = [
            make_candidate("5.00", "100", name="A", opis_id=1),
            make_candidate("3.00", "400", name="B", opis_id=2),
            make_candidate("4.00", "480", name="C", opis_id=3),
        ]

        plan = naive_baseline.solve(candidates, Decimal("900"))

        self.assertEqual(len(plan.stops), 1)
        self.assertEqual(plan.stops[0].distance_from_start_mi, Decimal("480"))
        self.assertEqual(plan.stops[0].opis_id, 3)

    def test_every_stop_is_a_complete_fill(self):
        candidates = [
            make_candidate("5.00", "100", name="A", opis_id=1),
            make_candidate("3.00", "400", name="B", opis_id=2),
            make_candidate("4.00", "480", name="C", opis_id=3),
            make_candidate("2.00", "900", name="D", opis_id=4),
        ]

        plan = naive_baseline.solve(candidates, Decimal("1400"))

        self.assertTrue(len(plan.stops) >= 1)
        fuel_on_arrival = Decimal("500") - plan.stops[0].distance_from_start_mi
        self.assertEqual(plan.stops[0].gallons * Decimal(10), Decimal("500") - fuel_on_arrival)
        for stop in plan.stops:
            self.assertIsNotNone(stop.opis_id)

    def test_no_purchase_recorded_at_start(self):
        candidates = [make_candidate("3.00", "50", name="C1", opis_id=1)]

        plan = naive_baseline.solve(
            candidates, Decimal("520"), starting_fuel=Decimal("0.5")
        )

        self.assertTrue(all(s.opis_id is not None for s in plan.stops))
        self.assertEqual(len(plan.stops), 1)
        self.assertEqual(plan.stops[0].distance_from_start_mi, Decimal("50"))


class InfeasibilityTests(SimpleTestCase):
    def test_infeasible_raises_with_gap_to_next_node(self):
        candidates = [make_candidate("3.00", "600", name="TOO_FAR", opis_id=1)]

        with self.assertRaises(InfeasibleRouteError) as ctx:
            naive_baseline.solve(candidates, Decimal("900"))

        err = ctx.exception
        self.assertEqual(err.from_station, "START")
        self.assertEqual(err.to_station, "TOO_FAR")
        self.assertEqual(err.gap_mi, Decimal("600"))
        self.assertEqual(err.max_range_mi, Decimal("500"))

    def test_zero_starting_fuel_cannot_reach_anything(self):
        with self.assertRaises(InfeasibleRouteError):
            naive_baseline.solve([], Decimal("100"), starting_fuel=Decimal("0.0"))


class NoStopNeededTests(SimpleTestCase):
    def test_route_completable_on_start_tank_needs_no_stops(self):
        plan = naive_baseline.solve([], Decimal("480"))

        self.assertEqual(plan.stops, [])
        self.assertEqual(plan.total_cost, Decimal("0"))
        self.assertEqual(plan.total_gallons, Decimal("0"))


class InvalidInputTests(SimpleTestCase):
    def test_starting_fuel_out_of_bounds_is_invalid(self):
        with self.assertRaises(InvalidRouteInputError):
            naive_baseline.solve([], Decimal("100"), starting_fuel=Decimal("1.5"))

    def test_negative_total_route_mi_is_invalid(self):
        with self.assertRaises(InvalidRouteInputError):
            naive_baseline.solve([], Decimal("-5"))


class SavingsTests(SimpleTestCase):
    def test_compute_savings_amount_and_percent(self):
        naive = FuelPlan(stops=[], total_cost=Decimal("300"), total_gallons=Decimal("100"))
        optimal = FuelPlan(stops=[], total_cost=Decimal("240"), total_gallons=Decimal("80"))

        savings = compute_savings(optimal, naive)

        self.assertIsInstance(savings, Savings)
        self.assertEqual(savings.amount, Decimal("60"))
        self.assertEqual(savings.percent, Decimal("0.2"))
        self.assertEqual(savings.naive_total_cost, Decimal("300"))
        self.assertEqual(savings.naive_total_gallons, Decimal("100"))
        self.assertEqual(savings.naive_stop_count, len(naive.stops))

    def test_compute_savings_zero_naive_cost_returns_none_percent(self):
        candidates = []
        naive = naive_baseline.solve(candidates, Decimal("100"))
        optimal = solve(candidates, Decimal("100"))

        savings = compute_savings(optimal, naive)

        self.assertEqual(savings.amount, Decimal("0"))
        self.assertIsNone(savings.percent)


class BaselineIsUpperBoundTests(SimpleTestCase):
    """The baseline is a price-blind upper bound: it must never cost less
    than the real solver's optimal plan on a landscape where both are
    feasible. A negative savings figure would mean one of the two is wrong."""

    def test_baseline_is_never_cheaper_than_optimal(self):
        candidates = [
            make_candidate("5.00", "100", name="A", opis_id=1),
            make_candidate("2.00", "300", name="B", opis_id=2),
            make_candidate("4.50", "480", name="C", opis_id=3),
            make_candidate("1.50", "700", name="D", opis_id=4),
        ]

        naive_plan = naive_baseline.solve(candidates, Decimal("900"))
        optimal_plan = solve(candidates, Decimal("900"))

        self.assertGreaterEqual(naive_plan.total_cost, optimal_plan.total_cost)
