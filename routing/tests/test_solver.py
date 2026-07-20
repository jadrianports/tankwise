from decimal import Decimal

from django.test import SimpleTestCase

from routing.services import Candidate, PurchaseReason, solve
from routing.services.exceptions import InfeasibleRouteError, InvalidRouteInputError


def make_candidate(price, distance, *, name="STOP", opis_id=1):
    return Candidate(
        name=name,
        opis_id=opis_id,
        price_per_gallon=Decimal(str(price)),
        distance_from_start_mi=Decimal(str(distance)),
    )


class BoundaryAndFeasibilityTests(SimpleTestCase):
    """Required cases 1, 3, 4: the exact-500 boundary, a
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
    """Required case 2, plus vector 15: a gap between along-route
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
    """Required case 5 (the greedy trap) plus additional case 6
    (cheaper-before-finish ordering) and 11 (unsorted input)."""

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
    """Additional case 7 (tie-break nearest at equal price) and case
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
    precision -- the solver never quantizes to cents."""

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


class StartingFuelTests(SimpleTestCase):
    """Regression coverage for the START-node landmine: a partial start
    tank must never be treated as a free, purchasable full tank."""

    def test_explicit_full_tank_matches_omitted_default(self):
        candidates = [
            make_candidate("3.00", "400", name="NEAR_EXPENSIVE", opis_id=1),
            make_candidate("2.00", "800", name="FAR_CHEAP", opis_id=2),
        ]

        explicit = solve(candidates, Decimal("1000"), starting_fuel=Decimal(1))
        omitted = solve(candidates, Decimal("1000"))

        self.assertEqual(explicit.stops, omitted.stops)
        self.assertEqual(explicit.total_cost, omitted.total_cost)
        self.assertEqual(explicit.total_gallons, omitted.total_gallons)

    def test_no_phantom_start_stop_across_starting_fuel_values(self):
        # A single real station well within reach at every tested
        # starting_fuel; the finish is reachable from that station on a
        # full tank, so every one of these values produces a plan with
        # exactly one real stop.
        candidates = [make_candidate("3.00", "50", name="C1", opis_id=1)]

        for starting_fuel in (Decimal("0.25"), Decimal("0.5"), Decimal("0.99")):
            with self.subTest(starting_fuel=starting_fuel):
                plan = solve(candidates, Decimal("520"), starting_fuel=starting_fuel)

                self.assertTrue(all(s.opis_id is not None for s in plan.stops))
                self.assertEqual(len(plan.stops), 1)
                self.assertEqual(plan.stops[0].opis_id, 1)

    def test_zero_starting_fuel_cannot_reach_anything(self):
        # Zero fuel at a non-purchasable origin is genuinely infeasible --
        # the old solver would instead have granted a free full tank.
        with self.assertRaises(InfeasibleRouteError):
            solve([], Decimal("100"), starting_fuel=Decimal("0.0"))

    def test_reduced_reachability_raises_with_partial_range_not_tank_capacity(self):
        candidates = [make_candidate("3.00", "400", name="A", opis_id=1)]

        with self.assertRaises(InfeasibleRouteError) as ctx:
            solve(
                candidates,
                Decimal("900"),
                tank_range_mi=Decimal("500"),
                mpg=Decimal("10"),
                starting_fuel=Decimal("0.5"),
            )

        err = ctx.exception
        self.assertEqual(err.from_station, "START")
        self.assertEqual(err.to_station, "A")
        self.assertEqual(err.gap_mi, Decimal("400"))
        self.assertEqual(err.max_range_mi, Decimal("250"))

    def test_partial_tank_purchase_happens_at_the_real_station(self):
        candidates = [make_candidate("3.00", "50", name="C1", opis_id=1)]

        plan = solve(
            candidates,
            Decimal("520"),
            tank_range_mi=Decimal("500"),
            mpg=Decimal("10"),
            starting_fuel=Decimal("0.5"),
        )

        self.assertEqual(len(plan.stops), 1)
        self.assertEqual(plan.stops[0].opis_id, 1)
        self.assertEqual(plan.stops[0].distance_from_start_mi, Decimal("50"))
        self.assertEqual(plan.stops[0].gallons, Decimal("27"))
        self.assertEqual(plan.total_cost, Decimal("81.00"))

    def test_starting_fuel_below_zero_is_invalid(self):
        with self.assertRaises(InvalidRouteInputError):
            solve([], Decimal("100"), starting_fuel=Decimal("-0.1"))

    def test_starting_fuel_above_one_is_invalid(self):
        with self.assertRaises(InvalidRouteInputError):
            solve([], Decimal("100"), starting_fuel=Decimal("1.5"))


class RationaleTests(SimpleTestCase):
    """Coverage for FuelStop's structured, branch-recorded rationale
    fields: purchase_reason, the aimed-at station, skipped-candidate
    context, and corridor price context."""

    def test_fill_to_continue_reach_cheaper_and_reach_finish_reasons(self):
        c1 = make_candidate("5.00", "50", name="C1", opis_id=1)
        c2 = make_candidate("6.00", "340", name="C2", opis_id=2)
        c3 = make_candidate("1.00", "500", name="C3", opis_id=3)

        plan = solve(
            [c1, c2, c3],
            Decimal("540"),
            tank_range_mi=Decimal("300"),
            mpg=Decimal("10"),
        )

        self.assertEqual(len(plan.stops), 3)
        stop1, stop2, stop3 = plan.stops

        self.assertEqual(stop1.opis_id, 1)
        self.assertEqual(stop1.purchase_reason, PurchaseReason.FILL_TO_CONTINUE)
        self.assertEqual(stop1.reason_target_opis_id, 2)
        self.assertEqual(stop1.reason_target_name, "C2")
        self.assertEqual(stop1.gallons, Decimal("5"))
        self.assertEqual(stop1.cost, Decimal("25.00"))
        self.assertEqual(stop1.skipped_count, 0)
        self.assertIsNone(stop1.skipped_avg_price)
        self.assertEqual(stop1.price_percentile, Decimal(1) / Decimal(3))
        self.assertEqual(stop1.corridor_avg_price, Decimal("4.00"))

        self.assertEqual(stop2.opis_id, 2)
        self.assertEqual(stop2.purchase_reason, PurchaseReason.REACH_CHEAPER_STOP)
        self.assertEqual(stop2.reason_target_opis_id, 3)
        self.assertEqual(stop2.reason_target_name, "C3")
        self.assertEqual(stop2.gallons, Decimal("15"))
        self.assertEqual(stop2.cost, Decimal("90.00"))
        self.assertEqual(stop2.skipped_count, 0)
        self.assertIsNone(stop2.skipped_avg_price)
        self.assertEqual(stop2.price_percentile, Decimal(2) / Decimal(3))
        self.assertEqual(stop2.corridor_avg_price, Decimal("4.00"))

        self.assertEqual(stop3.opis_id, 3)
        self.assertEqual(stop3.purchase_reason, PurchaseReason.REACH_FINISH)
        self.assertIsNone(stop3.reason_target_opis_id)
        self.assertIsNone(stop3.reason_target_name)
        self.assertEqual(stop3.gallons, Decimal("4"))
        self.assertEqual(stop3.cost, Decimal("4.00"))
        self.assertEqual(stop3.skipped_count, 0)
        self.assertIsNone(stop3.skipped_avg_price)
        self.assertEqual(stop3.price_percentile, Decimal(0))
        self.assertEqual(stop3.corridor_avg_price, Decimal("4.00"))

        self.assertEqual(plan.total_cost, Decimal("119.00"))
        self.assertEqual(plan.total_gallons, Decimal("24"))

    def test_top_up_at_cheapest_reason(self):
        d1 = make_candidate("1.00", "50", name="D1", opis_id=1)
        d2 = make_candidate("3.00", "220", name="D2", opis_id=2)

        plan = solve(
            [d1, d2],
            Decimal("400"),
            tank_range_mi=Decimal("200"),
            mpg=Decimal("10"),
        )

        self.assertEqual(len(plan.stops), 2)
        stop1, stop2 = plan.stops

        self.assertEqual(stop1.opis_id, 1)
        self.assertEqual(stop1.purchase_reason, PurchaseReason.TOP_UP_AT_CHEAPEST)
        self.assertEqual(stop1.reason_target_opis_id, 2)
        self.assertEqual(stop1.reason_target_name, "D2")
        self.assertEqual(stop1.gallons, Decimal("5"))
        self.assertEqual(stop1.cost, Decimal("5.00"))
        self.assertEqual(stop1.price_percentile, Decimal(0))
        self.assertEqual(stop1.corridor_avg_price, Decimal("2.00"))

        self.assertEqual(stop2.purchase_reason, PurchaseReason.REACH_FINISH)
        self.assertIsNone(stop2.reason_target_opis_id)

    def test_skipped_count_and_avg_price_between_stops(self):
        x1 = make_candidate("4.00", "200", name="X1", opis_id=1)
        x2 = make_candidate("2.00", "450", name="X2", opis_id=2)

        plan = solve(
            [x1, x2],
            Decimal("900"),
            tank_range_mi=Decimal("500"),
            mpg=Decimal("10"),
        )

        # Only X2 is ever purchased -- the solver hops directly to it from
        # START since it is the cheapest reachable candidate, skipping the
        # nearer, pricier X1 without ever stopping there.
        self.assertEqual(len(plan.stops), 1)
        stop = plan.stops[0]

        self.assertEqual(stop.opis_id, 2)
        self.assertEqual(stop.purchase_reason, PurchaseReason.REACH_FINISH)
        self.assertEqual(stop.skipped_count, 1)
        self.assertEqual(stop.skipped_avg_price, Decimal("4.00"))
        self.assertEqual(stop.price_percentile, Decimal(0))
        self.assertEqual(stop.corridor_avg_price, Decimal("3.00"))

    def test_fuel_stop_rationale_fields_default(self):
        from routing.services.solver import FuelStop

        stop = FuelStop(
            name="x",
            opis_id=1,
            price_per_gallon=Decimal(1),
            distance_from_start_mi=Decimal(1),
            gallons=Decimal(1),
            cost=Decimal(1),
        )

        self.assertIsNone(stop.purchase_reason)
        self.assertIsNone(stop.reason_target_opis_id)
        self.assertIsNone(stop.reason_target_name)
        self.assertEqual(stop.skipped_count, 0)
        self.assertIsNone(stop.skipped_avg_price)
        self.assertIsNone(stop.price_percentile)
        self.assertIsNone(stop.corridor_avg_price)
