"""Endpoint tests for `POST /api/route` (call-budget and error scenarios).

The Mapbox transport boundary (`routing.services.mapbox._SESSION.get`) is
always mocked -- no live network call is ever performed, and both
`get_routes()` and `geocode()` share this single mock target, so a
scenario's `mock_get.call_count` is the exact external-call budget.
Uses DRF `APITestCase` (this repo's first) -- it exercises full DRF request
dispatch, unlike the `SimpleTestCase` used for the pure service-layer
tests.
"""
import json
import math
from decimal import Decimal
from pathlib import Path
from unittest import mock

import requests
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from rest_framework import status
from rest_framework.test import APITestCase
from shapely.geometry import LineString

from routing.models import GeocodePrecision, GeocodeStatus, Station
from routing.services.corridor import reset_index
from routing.services.exceptions import InfeasibleRouteError
from routing.services.mapbox import Route
from routing.services.solver import Candidate, FuelPlan
from routing.timing import ServerTiming
from routing.views import RouteView

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

with open(FIXTURES_DIR / "mapbox_directions_response.json", encoding="utf-8") as f:
    DIRECTIONS_FIXTURE = json.load(f)

with open(FIXTURES_DIR / "mapbox_geocoding_response.json", encoding="utf-8") as f:
    GEOCODING_FIXTURE = json.load(f)

ROUTE_COORDS = DIRECTIONS_FIXTURE["routes"][0]["geometry"]["coordinates"]
# The 4th route vertex, used as a station's own lat/lng so it sits
# exactly on the route (perpendicular distance ~0) -- guaranteed inside
# the corridor regardless of tiering width.
STATION_LNG, STATION_LAT = ROUTE_COORDS[3]

MOCK_TARGET = "routing.services.mapbox._SESSION.get"
ROUTE_URL = "/api/route"

START_COORD = "41.8781,-87.6298"
FINISH_COORD = "38.6270,-90.1994"
START_ADDRESS = "233 S Wacker Dr, Chicago, IL"
FINISH_ADDRESS = "1 Busch Stadium Plaza, St Louis, MO"
# Regina, Saskatchewan -- outside the continental-US bbox even though
# Montreal/Toronto/Vancouver fall inside bbox.py's generous LAT_MAX
# (04-02 precedent).
NON_US_COORD = "50.4452,-104.6189"

# Distinct fake tokens for the D-14 full-response leak-regression test --
# using two different values (rather than reusing "test-token") makes it
# obvious which token, if either, actually appears in the response.
FAKE_SECRET = "sk.fake-secret-never-leak"
FAKE_PUBLIC = "pk.fake-public-token"


class _StubResponse:
    """Minimal stand-in for a `requests.Response` (mirrors test_mapbox.py)."""

    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _directions_response(payload=DIRECTIONS_FIXTURE):
    return _StubResponse(payload=payload)


def _geocoding_response():
    return _StubResponse(payload=GEOCODING_FIXTURE)


def _long_directions_payload():
    """A directions payload whose total distance (600 mi) exceeds the
    500-mi tank range, so a single mid-route station forces exactly one
    fuel stop (scenario: "route long enough to force >=1 stop")."""
    payload = json.loads(json.dumps(DIRECTIONS_FIXTURE))
    payload["routes"][0]["distance"] = 600 * 1609.344
    return payload


def _no_route_payload():
    payload = json.loads(json.dumps(DIRECTIONS_FIXTURE))
    payload["code"] = "NoRoute"
    payload["routes"] = []
    return payload


def _dense_long_directions_payload(n=4000):
    """A 600-mi directions payload (same distance override as
    `_long_directions_payload`) whose geometry is replaced with `n`
    densely interpolated, wiggly points between the fixture's own
    start/finish -- simulating a full-resolution real-world route
    geometry, so `route_geometry`'s point-count reduction is
    observable end-to-end through the live endpoint."""
    payload = _long_directions_payload()
    start_lng, start_lat = ROUTE_COORDS[0]
    finish_lng, finish_lat = ROUTE_COORDS[-1]
    coords = []
    for i in range(n):
        t = i / (n - 1)
        lng = start_lng + (finish_lng - start_lng) * t
        lat = start_lat + (finish_lat - start_lat) * t
        lat += 0.05 * math.sin(t * 40) + 0.01 * math.sin(t * 137)
        coords.append([lng, lat])
    payload["routes"][0]["geometry"]["coordinates"] = coords
    return payload


def _make_station(
    opis_id,
    lat=STATION_LAT,
    lng=STATION_LNG,
    price="3.259",
    precision=GeocodePrecision.ROOFTOP,
):
    return Station.objects.create(
        opis_id=opis_id,
        name="Test Travel Center",
        address="I-55, EXIT 1",
        city="Anytown",
        state="IL",
        rack_id="100",
        retail_price=Decimal(price),
        geocode_status=GeocodeStatus.OK,
        geocode_precision=precision,
        latitude=Decimal(str(lat)),
        longitude=Decimal(str(lng)),
        observation_count=1,
        price_min=Decimal(price),
        price_max=Decimal(price),
    )


@override_settings(MAPBOX_TOKEN="test-token", MAPBOX_PUBLIC_TOKEN="pk.test-public-token")
class RouteViewCallBudgetTests(APITestCase):
    """Call budget: 1 call for coord+coord, 2 for mixed, 3 for
    address+address."""

    def setUp(self):
        cache.clear()
        reset_index()

    def test_coordinate_happy_path_single_call_and_full_contract(self):
        _make_station(701)

        with mock.patch(
            MOCK_TARGET, return_value=_directions_response(_long_directions_payload())
        ) as mock_get:
            response = self.client.post(
                ROUTE_URL,
                {"start": START_COORD, "finish": FINISH_COORD},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(mock_get.call_count, 1)

        body = response.data
        self.assertTrue(body["fuel_stops"])
        self.assertIn("total_cost", body)
        self.assertIn("total_gallons", body)
        self.assertIn("total_route_mi", body)
        self.assertIn("route_geometry", body)
        self.assertIn("map_url", body)
        self.assertIsNotNone(body["map_url"])

    def test_dense_route_geometry_is_simplified_in_response(self):
        """`route_geometry` in the live response is far smaller than the
        route's raw geometry, with the exact start/finish endpoints
        preserved and [lng, lat] order unchanged."""
        dense_payload = _dense_long_directions_payload(n=4000)
        raw_coords = dense_payload["routes"][0]["geometry"]["coordinates"]
        # Place the station at the dense route's own midpoint vertex so
        # it sits on the (wiggly) corridor regardless of tiering width --
        # STATION_LAT/STATION_LNG are derived from the small default
        # fixture's geometry, not this test's replaced dense one.
        mid_lng, mid_lat = raw_coords[len(raw_coords) // 2]
        _make_station(703, lat=mid_lat, lng=mid_lng)

        with mock.patch(
            MOCK_TARGET, return_value=_directions_response(dense_payload)
        ) as mock_get:
            response = self.client.post(
                ROUTE_URL,
                {"start": START_COORD, "finish": FINISH_COORD},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(mock_get.call_count, 1)

        geometry = response.data["route_geometry"]
        self.assertLess(len(geometry), 0.25 * len(raw_coords))
        self.assertEqual(geometry[0], raw_coords[0])
        self.assertEqual(geometry[-1], raw_coords[-1])

    def test_address_happy_path_three_calls(self):
        with mock.patch(
            MOCK_TARGET,
            side_effect=[
                _geocoding_response(),
                _geocoding_response(),
                _directions_response(),
            ],
        ) as mock_get:
            response = self.client.post(
                ROUTE_URL,
                {"start": START_ADDRESS, "finish": FINISH_ADDRESS},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(mock_get.call_count, 3)

    def test_mixed_coordinate_and_address_two_calls(self):
        with mock.patch(
            MOCK_TARGET,
            side_effect=[_geocoding_response(), _directions_response()],
        ) as mock_get:
            response = self.client.post(
                ROUTE_URL,
                {"start": START_ADDRESS, "finish": FINISH_COORD},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(mock_get.call_count, 2)


@override_settings(MAPBOX_TOKEN="test-token", MAPBOX_PUBLIC_TOKEN="pk.test-public-token")
class RouteViewCacheTests(APITestCase):
    """An identical repeat is served from cache with zero
    additional Mapbox calls."""

    def setUp(self):
        cache.clear()
        reset_index()

    def test_repeat_request_hits_cache_with_zero_additional_calls(self):
        _make_station(702)
        payload = {"start": START_COORD, "finish": FINISH_COORD}

        with mock.patch(
            MOCK_TARGET, return_value=_directions_response(_long_directions_payload())
        ) as mock_get:
            first = self.client.post(ROUTE_URL, payload, format="json")
            second = self.client.post(ROUTE_URL, payload, format="json")

        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(first.data, second.data)


@override_settings(MAPBOX_TOKEN="test-token", MAPBOX_PUBLIC_TOKEN="pk.test-public-token")
class RouteViewValidationErrorTests(APITestCase):
    """Invalid/missing/non-US input returns 400."""

    def setUp(self):
        cache.clear()
        reset_index()

    def test_missing_finish_returns_400_invalid_input(self):
        with mock.patch(MOCK_TARGET) as mock_get:
            response = self.client.post(
                ROUTE_URL, {"start": START_COORD}, format="json"
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["error"]["code"], "invalid_input")
        mock_get.assert_not_called()

    def test_malformed_start_returns_400_invalid_input(self):
        with mock.patch(MOCK_TARGET) as mock_get:
            response = self.client.post(
                ROUTE_URL,
                {"start": 12345, "finish": FINISH_COORD},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["error"]["code"], "invalid_input")
        mock_get.assert_not_called()

    def test_non_us_coordinate_returns_400_with_no_directions_call(self):
        with mock.patch(MOCK_TARGET) as mock_get:
            response = self.client.post(
                ROUTE_URL,
                {"start": NON_US_COORD, "finish": FINISH_COORD},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            response.data["error"]["code"], {"invalid_input", "out_of_bounds"}
        )
        mock_get.assert_not_called()


@override_settings(MAPBOX_TOKEN="test-token", MAPBOX_PUBLIC_TOKEN="pk.test-public-token")
class RouteViewDomainErrorTests(APITestCase):
    """A route that cannot be found, or a >500-mi gap, returns a
    clear, specific 422; an upstream transport failure returns 502 with
    no token leak."""

    def setUp(self):
        cache.clear()
        reset_index()

    def test_no_route_returns_422_route_not_found(self):
        with mock.patch(
            MOCK_TARGET, return_value=_directions_response(_no_route_payload())
        ) as mock_get:
            response = self.client.post(
                ROUTE_URL,
                {"start": START_COORD, "finish": FINISH_COORD},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(response.data["error"]["code"], "route_not_found")
        mock_get.assert_called_once()

    def test_gap_exceeding_range_returns_422_infeasible_route(self):
        # No stations seeded -- the 600-mi route's START-to-FINISH gap
        # exceeds the 500-mi tank range with no candidate in between.
        with mock.patch(
            MOCK_TARGET, return_value=_directions_response(_long_directions_payload())
        ) as mock_get:
            response = self.client.post(
                ROUTE_URL,
                {"start": START_COORD, "finish": FINISH_COORD},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(response.data["error"]["code"], "infeasible_route")
        detail = response.data["error"]["detail"]
        self.assertIn("from_station", detail)
        self.assertIn("to_station", detail)
        self.assertIn("gap_mi", detail)
        self.assertIn("max_range_mi", detail)
        mock_get.assert_called_once()

    def test_upstream_transport_failure_returns_502_with_no_token_leak(self):
        with mock.patch(
            MOCK_TARGET, side_effect=requests.RequestException("boom")
        ) as mock_get:
            response = self.client.post(
                ROUTE_URL,
                {"start": START_COORD, "finish": FINISH_COORD},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertEqual(response.data["error"]["code"], "upstream_error")
        self.assertNotIn("test-token", json.dumps(response.data))
        mock_get.assert_called_once()


@override_settings(MAPBOX_TOKEN=FAKE_SECRET, MAPBOX_PUBLIC_TOKEN=FAKE_PUBLIC)
class TokenLeakRegressionTests(APITestCase):
    """D-14: the secret MAPBOX_TOKEN must never appear anywhere in a full
    /api/route response, while map_url must carry the public token."""

    def setUp(self):
        cache.clear()
        reset_index()

    def test_secret_token_absent_and_public_token_present_in_full_response(self):
        _make_station(704)

        with mock.patch(
            MOCK_TARGET, return_value=_directions_response(_long_directions_payload())
        ) as mock_get:
            response = self.client.post(
                ROUTE_URL,
                {"start": START_COORD, "finish": FINISH_COORD},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn(FAKE_SECRET, json.dumps(response.data))
        self.assertNotIn(FAKE_SECRET, response.content.decode())
        self.assertIn(FAKE_PUBLIC, response.data["map_url"])
        mock_get.assert_called_once()


_UNIT_VEHICLE = {
    "mpg": Decimal("10"),
    "tank_range_mi": Decimal("500"),
    "starting_fuel": Decimal("1"),
}


def _unit_route(index, total_route_mi, duration_s="100"):
    """A minimal `Route` for orchestration unit tests -- geometry is a
    throwaway two-point line since these tests patch
    `corridor.candidates` directly and never touch real geometry."""
    return Route(
        total_route_mi=Decimal(str(total_route_mi)),
        geometry=LineString([(0, 0), (1, 1)]),
        raw_coordinates=[[0, 0], [1, 1]],
        duration_s=Decimal(str(duration_s)),
        alternative_index=index,
    )


class RouteViewOrchestrationUnitTests(SimpleTestCase):
    """Direct unit tests of `RouteView._solve_all_alternatives` and
    `_select_winner` against hand-built `Route`/`Candidate` objects --
    sidesteps corridor geometry and the Mapbox transport boundary
    entirely by patching `corridor.candidates`, mirroring how
    `test_solver.py` exercises the pure solver directly. End-to-end
    HTTP-level coverage of the same alternatives-loop behaviors lives in
    `RouteViewMultiAlternativeTests` below."""

    def _view(self):
        view = RouteView()
        view._timing = ServerTiming()
        return view

    def test_cheapest_alternative_wins_when_all_feasible(self):
        cheap = Candidate(
            name="Cheap", opis_id=1, price_per_gallon=Decimal("2.50"),
            distance_from_start_mi=Decimal("400"),
        )
        pricey = Candidate(
            name="Pricey", opis_id=2, price_per_gallon=Decimal("4.00"),
            distance_from_start_mi=Decimal("400"),
        )
        view = self._view()
        routes = [_unit_route(0, 600), _unit_route(1, 600), _unit_route(2, 600)]

        with mock.patch(
            "routing.views.corridor.candidates",
            side_effect=[[pricey], [cheap], [pricey]],
        ):
            results = view._solve_all_alternatives(routes, _UNIT_VEHICLE)
            winner = view._select_winner(results)

        self.assertTrue(all(r.feasible for r in results))
        self.assertEqual(winner.index, 1)
        self.assertEqual(winner.plan.total_cost, min(r.plan.total_cost for r in results))

    def test_infeasible_alternative_is_skipped_when_another_solves(self):
        reachable = Candidate(
            name="Only", opis_id=1, price_per_gallon=Decimal("3.00"),
            distance_from_start_mi=Decimal("400"),
        )
        view = self._view()
        routes = [_unit_route(0, 600), _unit_route(1, 600), _unit_route(2, 600)]

        with mock.patch(
            "routing.views.corridor.candidates",
            side_effect=[[], [reachable], []],
        ):
            results = view._solve_all_alternatives(routes, _UNIT_VEHICLE)
            winner = view._select_winner(results)

        self.assertEqual([r.feasible for r in results], [False, True, False])
        self.assertEqual(winner.index, 1)

    def test_all_infeasible_raises_smallest_gap_across_alternatives(self):
        view = self._view()
        routes = [_unit_route(0, 900), _unit_route(1, 700), _unit_route(2, 800)]

        with mock.patch("routing.views.corridor.candidates", return_value=[]):
            with self.assertRaises(InfeasibleRouteError) as ctx:
                view._solve_all_alternatives(routes, _UNIT_VEHICLE)

        self.assertEqual(ctx.exception.gap_mi, Decimal("700"))

    def test_other_exception_types_propagate_uncaught(self):
        view = self._view()
        routes = [_unit_route(0, 600)]

        with mock.patch(
            "routing.views.corridor.candidates", side_effect=TypeError("boom")
        ):
            with self.assertRaises(TypeError):
                view._solve_all_alternatives(routes, _UNIT_VEHICLE)

    def _tied_result(self, index, total_cost, total_route_mi, duration_s):
        route = _unit_route(index, total_route_mi, duration_s=duration_s)
        plan = FuelPlan(
            stops=[], total_cost=Decimal(str(total_cost)), total_gallons=Decimal("0")
        )
        from routing.views import _AlternativeResult

        return _AlternativeResult(
            index=index, route=route, plan=plan, feasible=True, candidates=[]
        )

    def test_winner_selection_ties_break_by_route_miles_then_duration_then_index(self):
        view = self._view()

        # Level 2: cost tied, shorter route wins.
        results = [
            self._tied_result(0, "50.00", 300, "1000"),
            self._tied_result(1, "50.00", 250, "2000"),
        ]
        self.assertEqual(view._select_winner(results).index, 1)

        # Level 3: cost and miles tied, faster duration wins.
        results = [
            self._tied_result(0, "50.00", 300, "2000"),
            self._tied_result(1, "50.00", 300, "1000"),
        ]
        self.assertEqual(view._select_winner(results).index, 1)

        # Level 4: cost, miles, and duration all tied -- Mapbox's earlier
        # ordinal wins, regardless of list order.
        results = [
            self._tied_result(1, "50.00", 300, "1000"),
            self._tied_result(0, "50.00", 300, "1000"),
        ]
        self.assertEqual(view._select_winner(results).index, 0)
