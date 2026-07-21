"""Tests for the request/response serializers. No DB needed -- pure
serializer/field behavior.
"""
import datetime
import json
import math
from decimal import Decimal

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings
from shapely.geometry import LineString

from routing.serializers import (
    DEFAULT_MPG,
    DEFAULT_STARTING_FUEL,
    DEFAULT_TANK_RANGE_MI,
    FuelStopSerializer,
    RouteRequestSerializer,
    RouteResponseSerializer,
    price_freshness,
)
from routing.services.legs import Leg
from routing.services.mapbox import Route
from routing.services.naive_baseline import Savings
from routing.services.solver import Candidate, FuelPlan, FuelStop, PurchaseReason


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


class PriceFreshnessTests(SimpleTestCase):
    """price_freshness() surfaces the configured dataset vintage and its
    paired caveat, validated at point of use."""

    def test_returns_both_keys_from_settings(self):
        result = price_freshness()

        self.assertIn("price_as_of", result)
        self.assertIn("price_data_note", result)
        self.assertTrue(result["price_as_of"])
        self.assertTrue(result["price_data_note"])

    def test_blank_price_as_of_raises_improperly_configured(self):
        with override_settings(FUEL_PRICE_AS_OF=""):
            with self.assertRaises(ImproperlyConfigured):
                price_freshness()

    def test_configured_date_is_a_valid_iso_date(self):
        result = price_freshness()

        # Raises ValueError if not a valid ISO date -- assertion is
        # simply that this does not raise.
        datetime.date.fromisoformat(result["price_as_of"])


class FuelStopRationaleTests(SimpleTestCase):
    """Per-stop `rationale` object: structured facts only, reusing
    `_quantize_money` for every price, no prose."""

    def test_zero_skipped_count_yields_null_skipped_avg_price(self):
        stop = FuelStop(
            name="STOP1",
            opis_id=42,
            price_per_gallon=Decimal("3.00"),
            distance_from_start_mi=Decimal("100"),
            gallons=Decimal("30"),
            cost=Decimal("90.00"),
            purchase_reason=PurchaseReason.FILL_TO_CONTINUE,
            reason_target_opis_id=7,
            reason_target_name="NEXT",
            skipped_count=0,
            skipped_avg_price=None,
            price_percentile=Decimal("0.125"),
            corridor_avg_price=Decimal("3.40"),
        )

        data = FuelStopSerializer(stop, context={}).data
        rationale = data["rationale"]

        self.assertEqual(rationale["purchase_reason"], "fill_to_continue")
        self.assertEqual(rationale["reason_target_station_id"], 7)
        self.assertEqual(rationale["reason_target_name"], "NEXT")
        self.assertEqual(rationale["skipped_count"], 0)
        self.assertIsNone(rationale["skipped_avg_price"])
        self.assertEqual(rationale["price_percentile"], 12.5)
        self.assertEqual(rationale["corridor_avg_price"], "3.40")

    def test_endpoint_rule_purchase_has_null_reason_target(self):
        stop = FuelStop(
            name="STOP2",
            opis_id=43,
            price_per_gallon=Decimal("3.00"),
            distance_from_start_mi=Decimal("200"),
            gallons=Decimal("10"),
            cost=Decimal("30.00"),
            purchase_reason=PurchaseReason.REACH_FINISH,
            reason_target_opis_id=None,
            reason_target_name=None,
            skipped_count=2,
            skipped_avg_price=Decimal("3.512"),
            price_percentile=Decimal("0.5"),
            corridor_avg_price=Decimal("3.40"),
        )

        data = FuelStopSerializer(stop, context={}).data
        rationale = data["rationale"]

        self.assertIsNone(rationale["reason_target_station_id"])
        self.assertIsNone(rationale["reason_target_name"])
        self.assertEqual(rationale["skipped_count"], 2)
        self.assertEqual(rationale["skipped_avg_price"], "3.51")
        self.assertEqual(rationale["price_percentile"], 50.0)

    def test_v1_fuel_stop_keys_unchanged_alongside_rationale(self):
        stop = make_fuel_stop("3.00", "100", "30", "90.00", name="STOP1", opis_id=42)

        data = FuelStopSerializer(stop, context={}).data

        for key in (
            "name",
            "station_id",
            "location",
            "distance_from_start_mi",
            "price_per_gallon",
            "gallons",
            "cost",
        ):
            self.assertIn(key, data)
        self.assertEqual(data["cost"], "90.00")
        self.assertEqual(data["price_per_gallon"], "3.00")
        self.assertIn("rationale", data)


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


class RouteResponseSerializerTopLevelFieldsTests(SimpleTestCase):
    """vehicle/legs/savings/alternatives/price_as_of top-level fields --
    the D-04/D-11/D-16/D-27 response contract."""

    def _minimal_instance(self):
        route = Route(
            total_route_mi=Decimal("500"), geometry=LineString(), raw_coordinates=[]
        )
        plan = FuelPlan(stops=[], total_cost=Decimal("0"), total_gallons=Decimal("0"))
        return {"route": route, "plan": plan, "map_url": None}

    def test_minimal_v1_shaped_instance_serializes_without_raising(self):
        serializer = RouteResponseSerializer(self._minimal_instance(), context={})

        data = serializer.data

        self.assertIsNone(data["savings"])
        self.assertIsNone(data["savings_note"])
        self.assertEqual(data["alternatives"], [])
        self.assertEqual(data["alternatives_considered"], 0)
        self.assertIsNone(data["vehicle"])
        self.assertEqual(data["legs"], [])
        self.assertIn("price_as_of", data)
        self.assertIn("price_data_note", data)

    def test_vehicle_echo_includes_derived_starting_fuel_mi(self):
        instance = self._minimal_instance()
        instance["vehicle"] = {
            "mpg": Decimal("6"),
            "tank_range_mi": Decimal("500"),
            "starting_fuel": Decimal("0.5"),
        }

        data = RouteResponseSerializer(instance, context={}).data

        self.assertEqual(
            data["vehicle"],
            {
                "mpg": "6",
                "tank_range_mi": "500",
                "starting_fuel": "0.5",
                "starting_fuel_mi": "250",
            },
        )

    def test_legs_render_exact_key_set(self):
        instance = self._minimal_instance()
        instance["legs"] = [
            Leg(
                from_name="START",
                to_name="FINISH",
                distance_mi=Decimal("500"),
                duration_s=Decimal("18000"),
                gallons=Decimal("0"),
                cost=Decimal("0"),
            )
        ]

        data = RouteResponseSerializer(instance, context={}).data

        self.assertEqual(len(data["legs"]), 1)
        leg = data["legs"][0]
        self.assertEqual(
            set(leg.keys()), {"from", "to", "distance_mi", "duration_s", "gallons", "cost"}
        )
        self.assertEqual(leg["from"], "START")
        self.assertEqual(leg["to"], "FINISH")
        self.assertEqual(leg["duration_s"], 18000)

    def test_savings_percent_renders_as_percentage_number(self):
        instance = self._minimal_instance()
        instance["savings"] = Savings(
            amount=Decimal("10.00"),
            percent=Decimal("0.2"),
            naive_total_cost=Decimal("50.00"),
            naive_total_gallons=Decimal("15"),
            naive_stop_count=2,
        )

        data = RouteResponseSerializer(instance, context={}).data

        self.assertEqual(data["savings"]["percent"], 20.0)
        self.assertEqual(data["savings"]["amount"], "10.00")
        self.assertEqual(data["savings"]["naive_stop_count"], 2)

    def test_null_savings_with_note_leaves_rest_of_payload_intact(self):
        instance = self._minimal_instance()
        instance["savings"] = None
        instance["savings_note"] = "naive_plan_infeasible"

        data = RouteResponseSerializer(instance, context={}).data

        self.assertIsNone(data["savings"])
        self.assertEqual(data["savings_note"], "naive_plan_infeasible")
        self.assertEqual(data["total_route_mi"], "500")

    def test_alternatives_considered_matches_array_length(self):
        instance = self._minimal_instance()
        instance["alternatives"] = [
            {
                "total_route_mi": Decimal("500"),
                "duration_s": Decimal("18000"),
                "total_cost": Decimal("40.00"),
                "chosen": True,
                "feasible": True,
            },
            {
                "total_route_mi": Decimal("520"),
                "duration_s": Decimal("19000"),
                "total_cost": None,
                "chosen": False,
                "feasible": False,
            },
        ]

        data = RouteResponseSerializer(instance, context={}).data

        self.assertEqual(data["alternatives_considered"], len(data["alternatives"]))
        self.assertEqual(data["alternatives_considered"], 2)
        chosen_flags = [a["chosen"] for a in data["alternatives"]]
        self.assertEqual(chosen_flags.count(True), 1)

    def test_infeasible_alternative_renders_null_cost_not_omitted(self):
        instance = self._minimal_instance()
        instance["alternatives"] = [
            {
                "total_route_mi": Decimal("520"),
                "duration_s": Decimal("19000"),
                "total_cost": None,
                "chosen": False,
                "feasible": False,
            }
        ]

        data = RouteResponseSerializer(instance, context={}).data

        self.assertEqual(len(data["alternatives"]), 1)
        alt = data["alternatives"][0]
        self.assertIsNone(alt["total_cost"])
        self.assertFalse(alt["feasible"])
        no_geometry_or_stop_keys = {"geometry", "route_geometry", "stops", "fuel_stops"}
        self.assertFalse(no_geometry_or_stop_keys & set(alt.keys()))

    def test_total_duration_s_and_fuel_stop_count(self):
        instance = self._minimal_instance()
        instance["route"] = Route(
            total_route_mi=Decimal("500"),
            geometry=LineString(),
            raw_coordinates=[],
            duration_s=Decimal("18000"),
        )
        stop = make_fuel_stop("3.00", "100", "30", "90.00", name="STOP1", opis_id=42)
        instance["plan"] = FuelPlan(
            stops=[stop], total_cost=Decimal("90.00"), total_gallons=Decimal("30")
        )

        data = RouteResponseSerializer(instance, context={}).data

        self.assertEqual(data["total_duration_s"], 18000)
        self.assertEqual(data["fuel_stop_count"], 1)


class CandidateStationsTests(SimpleTestCase):
    """candidate_stations[] (D-09/D-10): lean five-field entries built
    from the winning alternative's corridor candidate list plus the
    orchestrator's opis_id-keyed coordinate map -- no `name`, no
    `address`. Reuses `_quantize_money`/`_quantize_miles`."""

    def _minimal_instance(self):
        route = Route(
            total_route_mi=Decimal("500"), geometry=LineString(), raw_coordinates=[]
        )
        plan = FuelPlan(stops=[], total_cost=Decimal("0"), total_gallons=Decimal("0"))
        return {"route": route, "plan": plan, "map_url": None}

    def test_exact_key_set_no_name_no_address(self):
        candidates = [
            Candidate(
                name="Cheap Gas",
                opis_id=42,
                price_per_gallon=Decimal("3.129"),
                distance_from_start_mi=Decimal("100.6"),
            )
        ]
        candidate_coords = {
            42: {"latitude": Decimal("39.0"), "longitude": Decimal("-88.0")}
        }

        data = RouteResponseSerializer(
            self._minimal_instance(),
            context={"candidates": candidates, "candidate_coords": candidate_coords},
        ).data

        self.assertEqual(len(data["candidate_stations"]), 1)
        entry = data["candidate_stations"][0]
        self.assertEqual(
            set(entry.keys()),
            {"station_id", "lat", "lng", "price_per_gallon", "distance_from_start_mi"},
        )
        self.assertEqual(entry["station_id"], 42)
        self.assertEqual(entry["lat"], 39.0)
        self.assertEqual(entry["lng"], -88.0)
        self.assertEqual(entry["price_per_gallon"], "3.13")
        self.assertEqual(entry["distance_from_start_mi"], "101")

    def test_null_opis_id_candidate_is_excluded(self):
        candidates = [
            Candidate(
                name="No DB Row",
                opis_id=None,
                price_per_gallon=Decimal("3.00"),
                distance_from_start_mi=Decimal("50"),
            )
        ]

        data = RouteResponseSerializer(
            self._minimal_instance(),
            context={"candidates": candidates, "candidate_coords": {}},
        ).data

        self.assertEqual(data["candidate_stations"], [])

    def test_candidate_with_no_matching_coords_is_excluded(self):
        candidates = [
            Candidate(
                name="Unresolved",
                opis_id=99,
                price_per_gallon=Decimal("3.00"),
                distance_from_start_mi=Decimal("50"),
            )
        ]

        data = RouteResponseSerializer(
            self._minimal_instance(),
            context={"candidates": candidates, "candidate_coords": {}},
        ).data

        self.assertEqual(data["candidate_stations"], [])

    def test_absent_context_renders_empty_list(self):
        data = RouteResponseSerializer(self._minimal_instance(), context={}).data

        self.assertEqual(data["candidate_stations"], [])


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


class ResponseContractTests(SimpleTestCase):
    """Pins the additive-only promise: every v1.0 response key/format is
    a proven subset of the v2 response, and the fully populated v2
    payload survives an end-to-end JSON round-trip. Builds `FuelStop`,
    `Leg`, `Savings`, and `Route` instances by hand -- no Mapbox call, DB
    row, or view invocation involved.
    """

    # The exact key sets a v1.0 client's request/response carried,
    # frozen here so a regression in either shows up as a failing test
    # rather than a silent contract break.
    V1_TOP_LEVEL_KEYS = {
        "start",
        "finish",
        "route_geometry",
        "total_route_mi",
        "fuel_stops",
        "total_cost",
        "total_gallons",
        "map_url",
    }
    V1_FUEL_STOP_KEYS = {
        "name",
        "station_id",
        "location",
        "distance_from_start_mi",
        "price_per_gallon",
        "gallons",
        "cost",
    }

    def _v1_shaped_instance_and_context(self):
        raw_coords = [[-87.6298, 41.8781], [-90.1994, 38.6270]]
        route = Route(
            total_route_mi=Decimal("500"),
            geometry=LineString(raw_coords),
            raw_coordinates=raw_coords,
        )
        stop = make_fuel_stop("3.129", "100.6", "30", "93.87", name="STOP1", opis_id=42)
        plan = FuelPlan(
            stops=[stop], total_cost=Decimal("93.87"), total_gallons=Decimal("30")
        )
        instance = {"route": route, "plan": plan, "map_url": "https://example.test/map"}
        context = {
            "stop_coords": {42: {"latitude": Decimal("39.0"), "longitude": Decimal("-88.0")}},
            "start_coords": {"latitude": Decimal("41.8781"), "longitude": Decimal("-87.6298")},
            "finish_coords": {"latitude": Decimal("38.6270"), "longitude": Decimal("-90.1994")},
        }
        return instance, context

    def _fully_populated_instance(self):
        instance, context = self._v1_shaped_instance_and_context()
        instance["vehicle"] = {
            "mpg": Decimal("6"),
            "tank_range_mi": Decimal("500"),
            "starting_fuel": Decimal("0.75"),
        }
        instance["plan"] = FuelPlan(
            stops=[
                FuelStop(
                    name="STOP1",
                    opis_id=42,
                    price_per_gallon=Decimal("3.129"),
                    distance_from_start_mi=Decimal("100.6"),
                    gallons=Decimal("30"),
                    cost=Decimal("93.87"),
                    purchase_reason=PurchaseReason.FILL_TO_CONTINUE,
                    reason_target_opis_id=7,
                    reason_target_name="NEXT",
                    skipped_count=1,
                    skipped_avg_price=Decimal("3.40"),
                    price_percentile=Decimal("0.25"),
                    corridor_avg_price=Decimal("3.35"),
                )
            ],
            total_cost=Decimal("93.87"),
            total_gallons=Decimal("30"),
        )
        instance["legs"] = [
            Leg(
                from_name="START",
                to_name="STOP1",
                distance_mi=Decimal("100.6"),
                duration_s=Decimal("6000"),
                gallons=Decimal("0"),
                cost=Decimal("0"),
            ),
            Leg(
                from_name="STOP1",
                to_name="FINISH",
                distance_mi=Decimal("399.4"),
                duration_s=Decimal("21000"),
                gallons=Decimal("30"),
                cost=Decimal("93.87"),
            ),
        ]
        instance["savings"] = Savings(
            amount=Decimal("10.13"),
            percent=Decimal("0.0975"),
            naive_total_cost=Decimal("104.00"),
            naive_total_gallons=Decimal("32"),
            naive_stop_count=2,
        )
        instance["alternatives"] = [
            {
                "total_route_mi": Decimal("500"),
                "duration_s": Decimal("27000"),
                "total_cost": Decimal("93.87"),
                "chosen": True,
                "feasible": True,
            },
            {
                "total_route_mi": Decimal("520"),
                "duration_s": Decimal("28500"),
                "total_cost": None,
                "chosen": False,
                "feasible": False,
            },
        ]
        return instance, context

    def test_v1_top_level_keys_are_subset_with_unchanged_formatting(self):
        instance, context = self._v1_shaped_instance_and_context()

        data = RouteResponseSerializer(instance, context=context).data

        self.assertTrue(self.V1_TOP_LEVEL_KEYS.issubset(data.keys()))
        # Same inputs, same formatting as before this plan's new fields
        # were added -- proven, not assumed.
        self.assertEqual(data["total_route_mi"], "500")
        self.assertEqual(data["total_cost"], "93.87")
        self.assertEqual(data["total_gallons"], "30.00")
        self.assertEqual(data["map_url"], "https://example.test/map")
        self.assertEqual(
            data["start"], {"latitude": "41.8781", "longitude": "-87.6298"}
        )
        self.assertEqual(data["route_geometry"][0], [-87.6298, 41.8781])

    def test_v1_fuel_stop_keys_are_subset_with_unchanged_formatting(self):
        instance, context = self._v1_shaped_instance_and_context()

        data = RouteResponseSerializer(instance, context=context).data
        stop = data["fuel_stops"][0]

        self.assertTrue(self.V1_FUEL_STOP_KEYS.issubset(stop.keys()))
        self.assertEqual(stop["name"], "STOP1")
        self.assertEqual(stop["station_id"], 42)
        self.assertEqual(stop["price_per_gallon"], "3.13")
        self.assertEqual(stop["gallons"], "30.00")
        self.assertEqual(stop["cost"], "93.87")
        self.assertEqual(stop["distance_from_start_mi"], "101")

    def test_v1_shaped_context_still_returns_candidate_stations_key(self):
        """A {start, finish}-only request (no candidates/candidate_coords
        in context) still returns every pre-existing top-level key,
        additively alongside the new candidate_stations[] key (D-09)."""
        instance, context = self._v1_shaped_instance_and_context()

        data = RouteResponseSerializer(instance, context=context).data

        self.assertIn("candidate_stations", data)
        self.assertEqual(data["candidate_stations"], [])
        self.assertTrue(self.V1_TOP_LEVEL_KEYS.issubset(data.keys()))

    def test_naive_plan_infeasible_savings_note_leaves_payload_valid(self):
        instance, context = self._v1_shaped_instance_and_context()
        instance["savings"] = None
        instance["savings_note"] = "naive_plan_infeasible"

        data = RouteResponseSerializer(instance, context=context).data

        self.assertIsNone(data["savings"])
        self.assertEqual(data["savings_note"], "naive_plan_infeasible")
        self.assertTrue(self.V1_TOP_LEVEL_KEYS.issubset(data.keys()))

    def test_exactly_one_alternative_chosen(self):
        instance, context = self._fully_populated_instance()

        data = RouteResponseSerializer(instance, context=context).data

        chosen = [a for a in data["alternatives"] if a["chosen"]]
        self.assertEqual(len(chosen), 1)

    def test_infeasible_alternative_total_cost_null_feasible_false(self):
        instance, context = self._fully_populated_instance()

        data = RouteResponseSerializer(instance, context=context).data

        infeasible = [a for a in data["alternatives"] if not a["feasible"]]
        self.assertEqual(len(infeasible), 1)
        self.assertIsNone(infeasible[0]["total_cost"])

    def test_fully_populated_payload_is_json_serializable_end_to_end(self):
        instance, context = self._fully_populated_instance()

        data = RouteResponseSerializer(instance, context=context).data

        # Raises TypeError if any raw Decimal (or other non-JSON-native
        # value) leaked through -- the assertion is that this does not
        # raise, proving the whole cached payload round-trips.
        serialized = json.dumps(data)
        round_tripped = json.loads(serialized)

        self.assertEqual(round_tripped["savings"]["percent"], 9.8)
        self.assertEqual(
            round_tripped["fuel_stops"][0]["rationale"]["price_percentile"], 25.0
        )
