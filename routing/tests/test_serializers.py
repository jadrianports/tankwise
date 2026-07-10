"""Tests for the request/response serializers (D-02, D-13, D-14, D-17,
Pitfall 4). No DB needed -- pure serializer/field behavior.
"""
from decimal import Decimal

from django.test import SimpleTestCase

from routing.serializers import (
    RouteRequestSerializer,
    RouteResponseSerializer,
)
from routing.services.mapbox import Route
from routing.services.solver import FuelPlan, FuelStop


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
    lat/lng (D-02)."""

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
    address, stripped of surrounding whitespace (D-02)."""

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
    ValidationError (D-17, API-03)."""

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
    outbound call (T-04-01)."""

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


class RouteResponseSerializerMoneyQuantizationTests(SimpleTestCase):
    """Money fields serialize as Decimal-as-string quantized to exactly
    2 places (D-14, Pitfall 4)."""

    def test_high_precision_cost_quantizes_to_two_places(self):
        route = Route(
            total_route_mi=Decimal("500"),
            geometry=None,
            raw_coordinates=[[-87.6298, 41.8781], [-90.1994, 38.6270]],
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

    def test_location_is_object_and_route_geometry_stays_raw_lnglat(self):
        route = Route(
            total_route_mi=Decimal("500"),
            geometry=None,
            raw_coordinates=[[-87.6298, 41.8781], [-90.1994, 38.6270]],
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
        self.assertEqual(data["route_geometry"], route.raw_coordinates)
        self.assertEqual(
            data["route_geometry"][0], [-87.6298, 41.8781]
        )  # [lng, lat], unchanged

    def test_missing_stop_coords_renders_none_location(self):
        route = Route(
            total_route_mi=Decimal("500"), geometry=None, raw_coordinates=[]
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
    """total_route_mi and total_gallons surface from Route/FuelPlan
    (D-15)."""

    def test_summary_fields_present(self):
        route = Route(
            total_route_mi=Decimal("512.75"), geometry=None, raw_coordinates=[]
        )
        plan = FuelPlan(stops=[], total_cost=Decimal("0"), total_gallons=Decimal("0"))

        serializer = RouteResponseSerializer(
            {"route": route, "plan": plan, "map_url": "https://example.test/map"},
            context={},
        )
        data = serializer.data

        self.assertEqual(data["total_route_mi"], "512.75")
        self.assertEqual(data["total_gallons"], "0")
        self.assertEqual(data["map_url"], "https://example.test/map")
        self.assertEqual(data["fuel_stops"], [])
