from dataclasses import dataclass, field
from decimal import Decimal

from django.test import SimpleTestCase

from routing.services import FuelPlan, FuelStop, Leg, build_legs
from routing.services.legs import build_waypoint_markers


@dataclass(frozen=True)
class FakeRoute:
    """Minimal stand-in for `routing.services.mapbox.Route` carrying only
    the fields `legs.py` reads."""

    total_route_mi: Decimal
    duration_s: Decimal = Decimal(0)
    annotation_distances: list = field(default_factory=list)
    annotation_durations: list = field(default_factory=list)


def make_stop(name, distance, gallons, cost, opis_id=1):
    return FuelStop(
        name=name,
        opis_id=opis_id,
        price_per_gallon=Decimal(cost) / Decimal(gallons) if gallons else Decimal(0),
        distance_from_start_mi=Decimal(str(distance)),
        gallons=Decimal(str(gallons)),
        cost=Decimal(str(cost)),
    )


# A 900-mile route split into three equal 300-mile / 1200-second segments,
# so interpolation can be checked against exact expected values.
ROUTE_900 = FakeRoute(
    total_route_mi=Decimal("900"),
    duration_s=Decimal("3600"),
    annotation_distances=[Decimal("300"), Decimal("300"), Decimal("300")],
    annotation_durations=[Decimal("1200"), Decimal("1200"), Decimal("1200")],
)


class LegCountTests(SimpleTestCase):
    def test_two_stops_produce_three_legs(self):
        plan = FuelPlan(
            stops=[
                make_stop("A", 300, 30, 90, opis_id=1),
                make_stop("B", 600, 30, 90, opis_id=2),
            ],
            total_cost=Decimal("180"),
            total_gallons=Decimal("60"),
        )

        legs = build_legs(ROUTE_900, plan)

        self.assertEqual(len(legs), 3)
        self.assertEqual(legs[0].from_name, "START")
        self.assertEqual(legs[0].to_name, "A")
        self.assertEqual(legs[1].from_name, "A")
        self.assertEqual(legs[1].to_name, "B")
        self.assertEqual(legs[2].from_name, "B")
        self.assertEqual(legs[2].to_name, "FINISH")

    def test_zero_stops_produce_one_leg_covering_whole_route(self):
        plan = FuelPlan(stops=[], total_cost=Decimal("0"), total_gallons=Decimal("0"))

        legs = build_legs(ROUTE_900, plan)

        self.assertEqual(len(legs), 1)
        self.assertEqual(legs[0].from_name, "START")
        self.assertEqual(legs[0].to_name, "FINISH")
        self.assertEqual(legs[0].distance_mi, Decimal("900"))

    def test_one_stop_produces_two_legs(self):
        plan = FuelPlan(
            stops=[make_stop("A", 480, 48, 192, opis_id=1)],
            total_cost=Decimal("192"),
            total_gallons=Decimal("48"),
        )

        legs = build_legs(ROUTE_900, plan)

        self.assertEqual(len(legs), 2)


class ReconciliationTests(SimpleTestCase):
    def test_distances_sum_to_total_route_mi(self):
        plan = FuelPlan(
            stops=[
                make_stop("A", 300, 30, 90, opis_id=1),
                make_stop("B", 600, 30, 90, opis_id=2),
            ],
            total_cost=Decimal("180"),
            total_gallons=Decimal("60"),
        )

        legs = build_legs(ROUTE_900, plan)

        self.assertEqual(sum((leg.distance_mi for leg in legs), Decimal(0)), Decimal("900"))

    def test_costs_and_gallons_reconcile_exactly_with_plan(self):
        plan = FuelPlan(
            stops=[
                make_stop("A", 300, 30, 90, opis_id=1),
                make_stop("B", 600, 30, 90, opis_id=2),
            ],
            total_cost=Decimal("180"),
            total_gallons=Decimal("60"),
        )

        legs = build_legs(ROUTE_900, plan)

        self.assertEqual(sum((leg.cost for leg in legs), Decimal(0)), plan.total_cost)
        self.assertEqual(sum((leg.gallons for leg in legs), Decimal(0)), plan.total_gallons)

    def test_first_leg_is_always_zero_cost_and_gallons(self):
        for stops in (
            [],
            [make_stop("A", 300, 30, 90, opis_id=1)],
            [
                make_stop("A", 300, 30, 90, opis_id=1),
                make_stop("B", 600, 30, 90, opis_id=2),
            ],
        ):
            with self.subTest(stop_count=len(stops)):
                plan = FuelPlan(
                    stops=stops,
                    total_cost=sum((s.cost for s in stops), Decimal(0)),
                    total_gallons=sum((s.gallons for s in stops), Decimal(0)),
                )

                legs = build_legs(ROUTE_900, plan)

                self.assertEqual(legs[0].gallons, Decimal(0))
                self.assertEqual(legs[0].cost, Decimal(0))

    def test_durations_sum_within_tolerance_of_route_duration(self):
        plan = FuelPlan(
            stops=[
                make_stop("A", 300, 30, 90, opis_id=1),
                make_stop("B", 600, 30, 90, opis_id=2),
            ],
            total_cost=Decimal("180"),
            total_gallons=Decimal("60"),
        )

        legs = build_legs(ROUTE_900, plan)
        total_duration = sum((leg.duration_s for leg in legs), Decimal(0))

        self.assertLessEqual(abs(total_duration - ROUTE_900.duration_s), Decimal("1"))


class InterpolationTests(SimpleTestCase):
    """A stop exactly on an annotation segment boundary exercises the
    exact-hit branch; a stop mid-segment exercises interpolation."""

    def test_stop_on_segment_boundary_is_exact(self):
        plan = FuelPlan(
            stops=[make_stop("A", 300, 30, 90, opis_id=1)],
            total_cost=Decimal("90"),
            total_gallons=Decimal("30"),
        )

        legs = build_legs(ROUTE_900, plan)

        self.assertEqual(legs[0].duration_s, Decimal("1200"))
        self.assertEqual(legs[1].duration_s, Decimal("2400"))

    def test_stop_mid_segment_interpolates(self):
        plan = FuelPlan(
            stops=[make_stop("A", 450, 45, 135, opis_id=1)],
            total_cost=Decimal("135"),
            total_gallons=Decimal("45"),
        )

        legs = build_legs(ROUTE_900, plan)

        # mile 450 sits halfway through the second 300-mile segment
        # (miles 300-600, seconds 1200-2400) -> 1800 seconds.
        self.assertEqual(legs[0].duration_s, Decimal("1800"))
        self.assertEqual(legs[1].duration_s, Decimal("1800"))


class EmptyAnnotationTests(SimpleTestCase):
    def test_empty_annotations_yield_none_durations(self):
        route = FakeRoute(total_route_mi=Decimal("900"), duration_s=Decimal("3600"))
        plan = FuelPlan(
            stops=[make_stop("A", 480, 48, 192, opis_id=1)],
            total_cost=Decimal("192"),
            total_gallons=Decimal("48"),
        )

        legs = build_legs(route, plan)

        self.assertTrue(all(leg.duration_s is None for leg in legs))


class WaypointMarkerTests(SimpleTestCase):
    """WAY-06/WAY-08: build_waypoint_markers() computes one marker per
    USER stop (letter-labeled), independent of legs[]/fuel_stops[]."""

    def test_three_stop_route_yields_markers_labeled_a_b_c(self):
        ordered_stop_coords = [
            (Decimal("41.8781"), Decimal("-87.6298")),
            (Decimal("39.7392"), Decimal("-104.9903")),
            (Decimal("34.0522"), Decimal("-118.2437")),
        ]
        leg_boundaries_mi = [Decimal("0"), Decimal("450")]

        markers = build_waypoint_markers(ROUTE_900, ordered_stop_coords, leg_boundaries_mi)

        self.assertEqual([m.label for m in markers], ["A", "B", "C"])
        self.assertEqual(markers[1].distance_from_start_mi, leg_boundaries_mi[1])
        self.assertEqual(markers[1].lat, float(ordered_stop_coords[1][0]))
        self.assertEqual(markers[1].lng, float(ordered_stop_coords[1][1]))
        # mile 450 sits halfway through the second 300-mile/1200-second
        # annotation segment (miles 300-600, seconds 1200-2400).
        self.assertEqual(markers[1].duration_s, Decimal("1800"))

    def test_marker_durations_are_monotonic_non_decreasing(self):
        ordered_stop_coords = [
            (Decimal("41.8781"), Decimal("-87.6298")),
            (Decimal("39.7392"), Decimal("-104.9903")),
            (Decimal("34.0522"), Decimal("-118.2437")),
        ]
        leg_boundaries_mi = [Decimal("0"), Decimal("450")]

        markers = build_waypoint_markers(ROUTE_900, ordered_stop_coords, leg_boundaries_mi)

        durations = [m.duration_s for m in markers]
        self.assertEqual(durations, sorted(durations))

    def test_two_point_route_yields_only_a_and_b(self):
        ordered_stop_coords = [
            (Decimal("41.8781"), Decimal("-87.6298")),
            (Decimal("38.6270"), Decimal("-90.1994")),
        ]
        leg_boundaries_mi = [Decimal("0")]

        markers = build_waypoint_markers(ROUTE_900, ordered_stop_coords, leg_boundaries_mi)

        self.assertEqual([m.label for m in markers], ["A", "B"])
        self.assertEqual(markers[0].name, "START")
        self.assertEqual(markers[0].distance_from_start_mi, Decimal("0"))
        self.assertEqual(markers[0].duration_s, Decimal("0"))
        self.assertEqual(markers[1].name, "FINISH")
        self.assertEqual(markers[1].distance_from_start_mi, Decimal("900"))
        self.assertEqual(markers[1].duration_s, Decimal("3600"))


class LegDataclassTests(SimpleTestCase):
    def test_leg_is_frozen_and_has_expected_fields(self):
        leg = Leg(
            from_name="START",
            to_name="FINISH",
            distance_mi=Decimal("900"),
            duration_s=Decimal("3600"),
            gallons=Decimal("0"),
            cost=Decimal("0"),
        )

        with self.assertRaises(Exception):
            leg.from_name = "X"
