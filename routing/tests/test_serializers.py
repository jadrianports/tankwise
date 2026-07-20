"""Tests for the request/response serializers. No DB needed -- pure
serializer/field behavior.
"""
import math
from decimal import Decimal

from django.test import SimpleTestCase
from shapely.geometry import LineString

from routing.serializers import (
    DEFAULT_MPG,
    DEFAULT_STARTING_FUEL,
    DEFAULT_TANK_RANGE_MI,
    RouteRequestSerializer,
    RouteResponseSerializer,
)
from routing.services.mapbox import Route
from routing.services.solver import FuelPlan, FuelStop


def _wiggly_route_coords(start, finish, n=4000):
    """Build `n` `[lng, lat]` points tracing a straight path from `start`
    to `finish` with sinusoidal perpendicular noise, simulating a dense,
    turn-heavy real-world route geometry -- used to prove `simplify_geometry`
    meaningfully reduces point count rather than trivially collapsing a
    near-straight line."""
    start_lng, start_lat = start
    finish_lng, finish_lat = finish
    coords = []
    for i in range(n):
        t = i / (n - 1)
        lng = start_lng + (finish_lng - start_lng) * t
        lat = start_lat + (finish_lat - start_lat) * t
        lat += 0.05 * math.sin(t * 40) + 0.01 * math.sin(t * 137)
        coords.append([lng, lat])
    return coords


def make_fuel_stop(price, distance, gallons, cost, *, name="STOP", opis_id=1):
    return FuelStop(
        name=name,
        opis_id=opis_id,
        price_per_gallon=Decimal(str(price)),
        distance_from_start_mi=Decimal(str(distance)),
        gallons=Decimal(str(gallons)),
        cost=Decimal(str(cost)),
    )


class LocationFieldCoordinateTests(SimpleTestCase):
    """Coordinate parsing: both "lat,lng" string and [lat, lng] list
    forms tag validated_data as kind == "coordinate" with Decimal
    lat/lng."""

    def test_string_and_list_coordinates_both_tagged_coordinate(self):
        serializer = RouteRequestSerializer(
            data={
                "start": "41.8781,-87.6298",
                "finish": [38.6270, -90.1994],
            }
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        start = serializer.validated_data["start"]
        finish = serializer.validated_data["finish"]

        self.assertEqual(start["kind"], "coordinate")
        self.assertIsInstance(start["lat"], Decimal)
        self.assertIsInstance(start["lng"], Decimal)
        self.assertEqual(start["lat"], Decimal("41.8781"))
        self.assertEqual(start["lng"], Decimal("-87.6298"))

        self.assertEqual(finish["kind"], "coordinate")
        self.assertEqual(finish["lat"], Decimal("38.6270"))
        self.assertEqual(finish["lng"], Decimal("-90.1994"))


class LocationFieldAddressTests(SimpleTestCase):
    """A non-empty, non-coordinate-shaped string is tagged as an
    address, stripped of surrounding whitespace."""

    def test_address_string_tagged_address_and_stripped(self):
        serializer = RouteRequestSerializer(
            data={
                "start": "  401 N Michigan Ave, Chicago, IL  ",
                "finish": "41.8781,-87.6298",
            }
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        start = serializer.validated_data["start"]

        self.assertEqual(start["kind"], "address")
        self.assertEqual(start["value"], "401 N Michigan Ave, Chicago, IL")


class LocationFieldMalformedInputTests(SimpleTestCase):
    """Empty, non-numeric-coordinate-shaped, and wrong-length list
    inputs all raise a ValidationError."""

    def test_empty_string_is_rejected(self):
        serializer = RouteRequestSerializer(
            data={"start": "", "finish": "41.8781,-87.6298"}
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("start", serializer.errors)

    def test_non_numeric_coordinate_list_is_rejected(self):
        serializer = RouteRequestSerializer(
            data={"start": ["abc", "def"], "finish": "41.8781,-87.6298"}
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("start", serializer.errors)

    def test_wrong_length_list_is_rejected(self):
        serializer = RouteRequestSerializer(
            data={
                "start": [41.8781, -87.6298, 0],
                "finish": "41.8781,-87.6298",
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("start", serializer.errors)


class LocationFieldBoundsRejectionTests(SimpleTestCase):
    """A coordinate outside the continental-US bbox raises a
    ValidationError."""

    def test_null_island_is_rejected(self):
        serializer = RouteRequestSerializer(
            data={"start": "0,0", "finish": "41.8781,-87.6298"}
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("start", serializer.errors)

    def test_canadian_point_is_rejected(self):
        # Regina, Saskatchewan -- north of the generous continental-US
        # bbox's LAT_MAX (49.4), unlike Montreal/Toronto/Vancouver which
        # sit just inside it.
        serializer = RouteRequestSerializer(
            data={"start": "50.4452,-104.6189", "finish": "41.8781,-87.6298"}
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("start", serializer.errors)


class LocationFieldAddressLengthTests(SimpleTestCase):
    """An address over MAX_ADDRESS_LENGTH chars is rejected before any
    outbound call."""

    def test_over_long_address_is_rejected(self):
        long_address = "A" * 300
        serializer = RouteRequestSerializer(
            data={"start": long_address, "finish": "41.8781,-87.6298"}
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("start", serializer.errors)

    def test_at_limit_address_is_accepted(self):
        at_limit_address = "A" * 256
        serializer = RouteRequestSerializer(
            data={"start": at_limit_address, "finish": "41.8781,-87.6298"}
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)


class VehicleRequestTests(SimpleTestCase):
    """Vehicle profile: optional, defaulted in one place, bounds-
    validated as Decimal."""

    def _base(self, vehicle=None):
        data = {"start": "40.0,-74.0", "finish": "41.0,-75.0"}
        if vehicle is not None:
            data["vehicle"] = vehicle
        return data

    def test_absent_vehicle_resolves_to_defaults(self):
        serializer = RouteRequestSerializer(data=self._base())

        self.assertTrue(serializer.is_valid(), serializer.errors)
        vehicle = serializer.validated_data["vehicle"]

        self.assertEqual(
            vehicle,
            {
                "mpg": DEFAULT_MPG,
                "tank_range_mi": DEFAULT_TANK_RANGE_MI,
                "starting_fuel": DEFAULT_STARTING_FUEL,
            },
        )
        for value in vehicle.values():
            self.assertIs(type(value), Decimal)

    def test_partial_vehicle_fills_remaining_defaults(self):
        serializer = RouteRequestSerializer(data=self._base({"mpg": 6}))

        self.assertTrue(serializer.is_valid(), serializer.errors)
        vehicle = serializer.validated_data["vehicle"]

        self.assertEqual(vehicle["mpg"], Decimal("6"))
        self.assertEqual(vehicle["tank_range_mi"], DEFAULT_TANK_RANGE_MI)
        self.assertEqual(vehicle["starting_fuel"], DEFAULT_STARTING_FUEL)
        for value in vehicle.values():
            self.assertIs(type(value), Decimal)

    def test_mpg_zero_and_just_under_one_are_rejected(self):
        for bad_mpg in (0, "0.5"):
            serializer = RouteRequestSerializer(data=self._base({"mpg": bad_mpg}))
            self.assertFalse(serializer.is_valid())
            self.assertIn("mpg", serializer.errors["vehicle"])

    def test_mpg_over_one_hundred_is_rejected(self):
        serializer = RouteRequestSerializer(data=self._base({"mpg": 101}))

        self.assertFalse(serializer.is_valid())
        self.assertIn("mpg", serializer.errors["vehicle"])

    def test_mpg_bounds_inclusive_accepted(self):
        for good_mpg in (1, 100):
            serializer = RouteRequestSerializer(data=self._base({"mpg": good_mpg}))
            self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_tank_range_mi_out_of_bounds_rejected(self):
        for bad_range in (19, 2001):
            serializer = RouteRequestSerializer(
                data=self._base({"tank_range_mi": bad_range})
            )
            self.assertFalse(serializer.is_valid())
            self.assertIn("tank_range_mi", serializer.errors["vehicle"])

    def test_tank_range_mi_bounds_inclusive_accepted(self):
        for good_range in (20, 2000):
            serializer = RouteRequestSerializer(
                data=self._base({"tank_range_mi": good_range})
            )
            self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_starting_fuel_out_of_bounds_rejected(self):
        for bad_fuel in ("-0.1", "1.01"):
            serializer = RouteRequestSerializer(
                data=self._base({"starting_fuel": bad_fuel})
            )
            self.assertFalse(serializer.is_valid())
            self.assertIn("starting_fuel", serializer.errors["vehicle"])

    def test_starting_fuel_bounds_inclusive_accepted(self):
        for good_fuel in ("0.0", "1.0"):
            serializer = RouteRequestSerializer(
                data=self._base({"starting_fuel": good_fuel})
            )
            self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_non_object_vehicle_rejected_with_400_class_error(self):
        for bad_vehicle in ("semi", ["mpg", 6]):
            serializer = RouteRequestSerializer(data=self._base(bad_vehicle))
            self.assertFalse(serializer.is_valid())
            self.assertIn("vehicle", serializer.errors)


class RouteResponseSerializerMoneyQuantizationTests(SimpleTestCase):
    """Money fields serialize as Decimal-as-string quantized to exactly
    2 places."""

    def test_high_precision_cost_quantizes_to_two_places(self):
        raw_coords = [[-87.6298, 41.8781], [-90.1994, 38.6270]]
        route = Route(
            total_route_mi=Decimal("500"),
            geometry=LineString(raw_coords),
            raw_coordinates=raw_coords,
        )
        stop = make_fuel_stop(
            "3.12345", "100", "30", "12.3459", name="STOP1", opis_id=42
        )
        plan = FuelPlan(
            stops=[stop], total_cost=Decimal("12.3459"), total_gallons=Decimal("30")
        )

        serializer = RouteResponseSerializer(
            {"route": route, "plan": plan, "map_url": "https://example.test/map"},
            context={
                "stop_coords": {42: {"latitude": Decimal("39.0"), "longitude": Decimal("-88.0")}},
                "start_coords": {"latitude": Decimal("41.8781"), "longitude": Decimal("-87.6298")},
                "finish_coords": {"latitude": Decimal("38.6270"), "longitude": Decimal("-90.1994")},
            },
        )
        data = serializer.data

        self.assertEqual(data["total_cost"], "12.35")
        self.assertEqual(data["fuel_stops"][0]["cost"], "12.35")
        self.assertEqual(data["fuel_stops"][0]["price_per_gallon"], "3.12")

    def test_location_is_object_and_route_geometry_stays_lnglat(self):
        # Only 2 vertices -- Douglas-Peucker always keeps both endpoints
        # of a 2-point line, so the simplified output equals the raw
        # coordinates here (the reduction case is covered separately by
        # RouteResponseSerializerGeometrySimplificationTests).
        raw_coords = [[-87.6298, 41.8781], [-90.1994, 38.6270]]
        route = Route(
            total_route_mi=Decimal("500"),
            geometry=LineString(raw_coords),
            raw_coordinates=raw_coords,
        )
        stop = make_fuel_stop("3.00", "100", "30", "90.00", name="STOP1", opis_id=42)
        plan = FuelPlan(
            stops=[stop], total_cost=Decimal("90.00"), total_gallons=Decimal("30")
        )

        serializer = RouteResponseSerializer(
            {"route": route, "plan": plan, "map_url": None},
            context={
                "stop_coords": {42: {"latitude": Decimal("39.0"), "longitude": Decimal("-88.0")}},
            },
        )
        data = serializer.data

        self.assertEqual(
            data["fuel_stops"][0]["location"],
            {"latitude": "39.0", "longitude": "-88.0"},
        )
        self.assertEqual(data["route_geometry"], raw_coords)
        self.assertEqual(
            data["route_geometry"][0], [-87.6298, 41.8781]
        )  # [lng, lat], unchanged

    def test_missing_stop_coords_renders_none_location(self):
        route = Route(
            total_route_mi=Decimal("500"), geometry=LineString(), raw_coordinates=[]
        )
        stop = make_fuel_stop("3.00", "100", "30", "90.00", name="STOP1", opis_id=99)
        plan = FuelPlan(
            stops=[stop], total_cost=Decimal("90.00"), total_gallons=Decimal("30")
        )

        serializer = RouteResponseSerializer(
            {"route": route, "plan": plan, "map_url": None}, context={}
        )
        data = serializer.data

        self.assertIsNone(data["fuel_stops"][0]["location"])


class RouteResponseSerializerSummaryFieldsTests(SimpleTestCase):
    """total_route_mi and total_gallons surface from Route/FuelPlan."""

    def test_summary_fields_present(self):
        route = Route(
            total_route_mi=Decimal("512.75"), geometry=LineString(), raw_coordinates=[]
        )
        plan = FuelPlan(stops=[], total_cost=Decimal("0"), total_gallons=Decimal("0"))

        serializer = RouteResponseSerializer(
            {"route": route, "plan": plan, "map_url": "https://example.test/map"},
            context={},
        )
        data = serializer.data

        self.assertEqual(data["total_route_mi"], "513")
        self.assertEqual(data["total_gallons"], "0.00")
        self.assertEqual(data["map_url"], "https://example.test/map")
        self.assertEqual(data["fuel_stops"], [])


class FuelStopDistanceFromStartTests(SimpleTestCase):
    """Each fuel_stops[] entry carries distance_from_start_mi, quantized
    identically to total_route_mi (ROUND_HALF_UP to the nearest whole
    mile)."""

    def test_whole_number_distance_serializes_unchanged(self):
        route = Route(
            total_route_mi=Decimal("500"), geometry=LineString(), raw_coordinates=[]
        )
        stop = make_fuel_stop("3.00", "100", "30", "90.00", name="STOP1", opis_id=42)
        plan = FuelPlan(
            stops=[stop], total_cost=Decimal("90.00"), total_gallons=Decimal("30")
        )

        serializer = RouteResponseSerializer(
            {"route": route, "plan": plan, "map_url": None}, context={}
        )
        data = serializer.data

        self.assertEqual(data["fuel_stops"][0]["distance_from_start_mi"], "100")

    def test_fractional_distance_rounds_half_up_to_whole_mile(self):
        route = Route(
            total_route_mi=Decimal("500"), geometry=LineString(), raw_coordinates=[]
        )
        stop = make_fuel_stop(
            "3.00", "100.6", "30", "90.00", name="STOP1", opis_id=42
        )
        plan = FuelPlan(
            stops=[stop], total_cost=Decimal("90.00"), total_gallons=Decimal("30")
        )

        serializer = RouteResponseSerializer(
            {"route": route, "plan": plan, "map_url": None}, context={}
        )
        data = serializer.data

        self.assertEqual(data["fuel_stops"][0]["distance_from_start_mi"], "101")


class RouteResponseSerializerGeometrySimplificationTests(SimpleTestCase):
    """`route_geometry` is simplified via `simplify_geometry` rather than
    returned as `route.raw_coordinates` verbatim -- a full-resolution
    route can be several thousand points, which would dominate the
    payload. Simplification must substantially shrink the point count
    while preserving the exact start/finish endpoints and [lng, lat]
    coordinate order."""

    def test_dense_route_geometry_is_simplified_with_endpoints_preserved(self):
        start = (-87.6298, 41.8781)  # Chicago, [lng, lat]
        finish = (-97.7431, 30.2672)  # Austin, [lng, lat]
        raw_coords = _wiggly_route_coords(start, finish, n=4000)
        route = Route(
            total_route_mi=Decimal("1100"),
            geometry=LineString(raw_coords),
            raw_coordinates=raw_coords,
        )
        plan = FuelPlan(stops=[], total_cost=Decimal("0"), total_gallons=Decimal("0"))

        serializer = RouteResponseSerializer(
            {"route": route, "plan": plan, "map_url": None}, context={}
        )
        data = serializer.data
        geometry = data["route_geometry"]

        # Far smaller than the raw point count -- strictly under 25%,
        # and in practice a couple of orders of magnitude smaller for a
        # long, turn-heavy route.
        self.assertLess(len(geometry), 0.25 * len(raw_coords))
        # Simplification actually happened (not a no-op passthrough).
        self.assertLess(len(geometry), len(raw_coords))

        # Exact start/finish endpoints preserved.
        self.assertEqual(geometry[0], raw_coords[0])
        self.assertEqual(geometry[-1], raw_coords[-1])

        # [lng, lat] order preserved -- longitude (~ -87 to -97) stays
        # first, latitude (~30 to 42) stays second.
        for lng, lat in geometry:
            self.assertTrue(-98 <= lng <= -87)
            self.assertTrue(29 <= lat <= 43)
