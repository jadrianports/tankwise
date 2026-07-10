from decimal import Decimal

from django.test import SimpleTestCase

from routing.services import Candidate, solve
from routing.services.exceptions import InfeasibleRouteError, InvalidRouteInputError


def make_candidate(price, distance, *, name="STOP", opis_id=1):
    return Candidate(
        name=name,
        opis_id=opis_id,
        price_per_gallon=Decimal(str(price)),
        distance_from_start_mi=Decimal(str(distance)),
    )


class BoundaryAndFeasibilityTests(SimpleTestCase):
    """OPS-03 required cases 1, 3, 4: the exact-500 boundary, a
    single-candidate route, and a sub-500 trip needing no stop at all."""

    def test_exact_500_boundary_is_feasible_not_infeasible(self):
        candidates = [make_candidate("3.00", "500", name="EDGE", opis_id=1)]

        plan = solve(candidates, Decimal("1000"))

        self.assertEqual(plan.total_cost, Decimal("150.00"))
        self.assertEqual(plan.total_gallons, Decimal("50"))
        self.assertEqual(len(plan.stops), 1)

    def test_single_candidate_buys_only_enough_to_finish(self):
        candidates = [make_candidate("3.00", "400", name="ONLY", opis_id=1)]

        plan = solve(candidates, Decimal("800"))

        self.assertEqual(len(plan.stops), 1)
        self.assertEqual(plan.stops[0].gallons, Decimal("30"))
        self.assertEqual(plan.total_cost, Decimal("90.00"))

    def test_sub_500_trip_needs_no_stop(self):
        plan = solve([], Decimal("480"))

        self.assertEqual(plan.stops, [])
        self.assertEqual(plan.total_cost, Decimal("0"))
        self.assertEqual(plan.total_gallons, Decimal("0"))


class InfeasibleRouteTests(SimpleTestCase):
    """OPS-03 required case 2, plus vector 15: a gap between along-route
    nodes that exceeds max range must raise InfeasibleRouteError with the
    full structured payload, not just the exception type."""

    def test_infeasible_gap_from_start_to_finish(self):
        with self.assertRaises(InfeasibleRouteError) as ctx:
            solve([], Decimal("600"))

        err = ctx.exception
        self.assertEqual(err.from_station, "START")
        self.assertEqual(err.to_station, "FINISH")
        self.assertEqual(err.gap_mi, Decimal("600"))
        self.assertEqual(err.max_range_mi, Decimal("500"))

    def test_first_leg_infeasible_from_start_to_station(self):
        candidates = [make_candidate("3.00", "600", name="TOO_FAR", opis_id=1)]

        with self.assertRaises(InfeasibleRouteError) as ctx:
            solve(candidates, Decimal("900"))

        err = ctx.exception
        self.assertEqual(err.from_station, "START")
        self.assertEqual(err.to_station, "TOO_FAR")
        self.assertEqual(err.gap_mi, Decimal("600"))
        self.assertEqual(err.max_range_mi, Decimal("500"))


class GreedyOptimalityTests(SimpleTestCase):
    """OPS-03 required case 5 (the greedy trap) plus additional case 6
    (cheaper-before-finish ordering) and 11 (unsorted input, D-07)."""

    def test_greedy_trap_prefers_cheaper_station_over_fill_up(self):
        candidates = [
            make_candidate("3.00", "400", name="NEAR_EXPENSIVE", opis_id=1),
            make_candidate("2.00", "800", name="FAR_CHEAP", opis_id=2),
        ]

        plan = solve(candidates, Decimal("1000"))

        self.assertEqual(plan.total_cost, Decimal("130.00"))
        self.assertEqual(plan.total_gallons, Decimal("50"))

    def test_greedy_trap_is_order_independent(self):
        candidates = [
            make_candidate("2.00", "800", name="FAR_CHEAP", opis_id=2),
            make_candidate("3.00", "400", name="NEAR_EXPENSIVE", opis_id=1),
        ]

        plan = solve(candidates, Decimal("1000"))

        self.assertEqual(plan.total_cost, Decimal("130.00"))
        self.assertEqual(plan.total_gallons, Decimal("50"))

    def test_cheaper_before_finish_beats_finish_first(self):
        candidates = [
            make_candidate("3.00", "450", name="FIRST", opis_id=1),
            make_candidate("2.80", "700", name="SECOND", opis_id=2),
        ]

        plan = solve(candidates, Decimal("800"))

        self.assertEqual(plan.total_cost, Decimal("88.00"))


class TieBreakAndEndpointTests(SimpleTestCase):
    """Additional case 7 (D-05 tie-break nearest at equal price) and case
    12 (endpoint rule buys only remaining/mpg, never a full tank)."""

    def test_tie_break_at_equal_price_picks_nearest(self):
        candidates = [
            make_candidate("2.00", "450", name="FAR_TIE", opis_id=1),
            make_candidate("2.00", "300", name="NEAR_TIE", opis_id=2),
        ]

        plan = solve(candidates, Decimal("800"))

        self.assertEqual(len(plan.stops), 1)
        self.assertEqual(plan.stops[0].distance_from_start_mi, Decimal("300"))
        self.assertEqual(plan.total_cost, Decimal("60.00"))

    def test_end_of_trip_no_overbuy(self):
        candidates = [make_candidate("3.00", "400", name="LAST", opis_id=1)]

        plan = solve(candidates, Decimal("850"))

        self.assertEqual(len(plan.stops), 1)
        self.assertEqual(plan.stops[0].gallons, Decimal("35"))
        self.assertEqual(plan.total_cost, Decimal("105.00"))


class PrecisionTests(SimpleTestCase):
    """Additional case 13: total_cost must retain full unrounded Decimal
    precision (D-09) -- the solver never quantizes to cents."""

    def test_total_cost_is_not_rounded_to_cents(self):
        candidates = [make_candidate("2.87912345", "400", name="PRECISE", opis_id=1)]

        plan = solve(candidates, Decimal("850"))

        self.assertEqual(plan.total_cost, Decimal("100.76932075"))
        self.assertNotEqual(plan.total_cost, Decimal("100.77"))


class InvalidInputTests(SimpleTestCase):
    """Additional case 14: defensive InvalidRouteInputError guards."""

    def test_negative_total_route_mi_is_invalid(self):
        with self.assertRaises(InvalidRouteInputError):
            solve([], Decimal("-5"))

    def test_zero_total_route_mi_is_invalid(self):
        with self.assertRaises(InvalidRouteInputError):
            solve([], Decimal("0"))

    def test_negative_candidate_price_is_invalid(self):
        candidates = [make_candidate("-1", "100", name="BAD_PRICE", opis_id=1)]

        with self.assertRaises(InvalidRouteInputError):
            solve(candidates, Decimal("500"))

    def test_candidate_distance_beyond_route_is_invalid(self):
        candidates = [make_candidate("3.00", "600", name="TOO_FAR", opis_id=1)]

        with self.assertRaises(InvalidRouteInputError):
            solve(candidates, Decimal("500"))

    def test_negative_candidate_distance_is_invalid(self):
        candidates = [make_candidate("3.00", "-10", name="NEGATIVE_POS", opis_id=1)]

        with self.assertRaises(InvalidRouteInputError):
            solve(candidates, Decimal("500"))
