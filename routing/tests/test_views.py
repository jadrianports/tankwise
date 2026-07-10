"""Endpoint tests for `POST /api/route` (D-18 scenarios, PERF-01, PERF-02).

The Mapbox transport boundary (`routing.services.mapbox.requests.get`) is
always mocked -- no live network call is ever performed, and both
`get_route()` and `geocode()` share this single mock target, so a
scenario's `mock_get.call_count` is the exact external-call budget
(PERF-01). Uses DRF `APITestCase` (this repo's first) per CLAUDE.md's
Django TestCase/APITestCase stack lock -- it exercises full DRF request
dispatch, unlike the `SimpleTestCase` used for the pure service-layer
tests.
"""
import json
from decimal import Decimal
from pathlib import Path
from unittest import mock

import requests
from django.core.cache import cache
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from routing.models import GeocodePrecision, GeocodeStatus, Station

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

MOCK_TARGET = "routing.services.mapbox.requests.get"
ROUTE_URL = "/api/route"

START_COORD = "41.8781,-87.6298"
FINISH_COORD = "38.6270,-90.1994"
START_ADDRESS = "233 S Wacker Dr, Chicago, IL"
FINISH_ADDRESS = "1 Busch Stadium Plaza, St Louis, MO"
# Regina, Saskatchewan -- outside the continental-US bbox even though
# Montreal/Toronto/Vancouver fall inside bbox.py's generous LAT_MAX
# (04-02 precedent).
NON_US_COORD = "50.4452,-104.6189"


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
    fuel stop (D-18 scenario 1: "route long enough to force >=1 stop")."""
    payload = json.loads(json.dumps(DIRECTIONS_FIXTURE))
    payload["routes"][0]["distance"] = 600 * 1609.344
    return payload


def _no_route_payload():
    payload = json.loads(json.dumps(DIRECTIONS_FIXTURE))
    payload["code"] = "NoRoute"
    payload["routes"] = []
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


@override_settings(MAPBOX_TOKEN="test-token")
class RouteViewCallBudgetTests(APITestCase):
    """PERF-01/API-05: 1 call for coord+coord, 2 for mixed, 3 for
    address+address."""

    def setUp(self):
        cache.clear()

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


@override_settings(MAPBOX_TOKEN="test-token")
class RouteViewCacheTests(APITestCase):
    """PERF-02: an identical repeat is served from cache with zero
    additional Mapbox calls."""

    def setUp(self):
        cache.clear()

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


@override_settings(MAPBOX_TOKEN="test-token")
class RouteViewValidationErrorTests(APITestCase):
    """API-03: invalid/missing/non-US input returns 400."""

    def setUp(self):
        cache.clear()

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


@override_settings(MAPBOX_TOKEN="test-token")
class RouteViewDomainErrorTests(APITestCase):
    """API-04: a route that cannot be found, or a >500-mi gap, returns a
    clear, specific 422; an upstream transport failure returns 502 with
    no token leak."""

    def setUp(self):
        cache.clear()

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
