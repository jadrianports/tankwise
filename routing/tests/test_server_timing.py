"""Endpoint integration tests for the `Server-Timing` header on
`POST /api/route` (FND-04).

Self-contained -- deliberately does NOT import from `test_views.py` so
this module runs independently of any other plan's edits to that file.
Mirrors its fixture-loading and mocking style.
"""
import json
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from routing.models import GeocodePrecision, GeocodeStatus, Station
from routing.services.corridor import reset_index

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


@override_settings(MAPBOX_TOKEN="test-token", MAPBOX_PUBLIC_TOKEN="pk.test-public")
class ServerTimingCacheMissTests(APITestCase):
    """D-07: a cache-miss 200 response carries route/corridor/solver/total,
    and the timing data never rides in the JSON body (D-09)."""

    def setUp(self):
        cache.clear()
        reset_index()

    def test_coordinate_happy_path_carries_stage_metrics_and_no_body_leak(self):
        _make_station(801)

        with mock.patch(
            MOCK_TARGET, return_value=_directions_response(_long_directions_payload())
        ):
            response = self.client.post(
                ROUTE_URL,
                {"start": START_COORD, "finish": FINISH_COORD},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("Server-Timing", response)
        header = response["Server-Timing"]
        self.assertIn("route;dur=", header)
        self.assertIn("corridor;dur=", header)
        self.assertIn("solver;dur=", header)
        self.assertIn("total;dur=", header)
        self.assertNotIn("dur=", json.dumps(response.data))


@override_settings(MAPBOX_TOKEN="test-token", MAPBOX_PUBLIC_TOKEN="pk.test-public")
class ServerTimingCacheHitTests(APITestCase):
    """D-08: a cache-hit response carries ONLY a cache metric."""

    def setUp(self):
        cache.clear()
        reset_index()

    def test_repeat_request_header_contains_only_cache_metric(self):
        _make_station(802)
        payload = {"start": START_COORD, "finish": FINISH_COORD}

        with mock.patch(
            MOCK_TARGET, return_value=_directions_response(_long_directions_payload())
        ) as mock_get:
            self.client.post(ROUTE_URL, payload, format="json")
            second = self.client.post(ROUTE_URL, payload, format="json")

        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        header = second["Server-Timing"]
        self.assertIn("cache;dur=", header)
        self.assertNotIn("route", header)
        self.assertNotIn("corridor", header)
        self.assertNotIn("solver", header)
        self.assertNotIn("total", header)


@override_settings(MAPBOX_TOKEN="test-token", MAPBOX_PUBLIC_TOKEN="pk.test-public")
class ServerTimingGeocodeTests(APITestCase):
    """D-07: geocode appears at most once even when both endpoints are
    addresses (Pitfall 6)."""

    def setUp(self):
        cache.clear()
        reset_index()

    def test_address_happy_path_has_exactly_one_geocode_metric(self):
        with mock.patch(
            MOCK_TARGET,
            side_effect=[
                _geocoding_response(),
                _geocoding_response(),
                _directions_response(),
            ],
        ):
            response = self.client.post(
                ROUTE_URL,
                {"start": START_ADDRESS, "finish": FINISH_ADDRESS},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        header = response["Server-Timing"]
        self.assertEqual(header.count("geocode"), 1)
        self.assertIn("route;dur=", header)
        self.assertIn("corridor;dur=", header)
        self.assertIn("solver;dur=", header)
        self.assertIn("total;dur=", header)


@override_settings(MAPBOX_TOKEN="test-token", MAPBOX_PUBLIC_TOKEN="pk.test-public")
class ServerTimingErrorTests(APITestCase):
    """D-10: an error response carries timings for the stages that ran
    before the failure."""

    def setUp(self):
        cache.clear()
        reset_index()

    def test_infeasible_route_carries_partial_route_and_corridor_timing(self):
        # No stations seeded -- the 600-mi route's START-to-FINISH gap
        # exceeds the 500-mi tank range with no candidate in between.
        with mock.patch(
            MOCK_TARGET, return_value=_directions_response(_long_directions_payload())
        ):
            response = self.client.post(
                ROUTE_URL,
                {"start": START_COORD, "finish": FINISH_COORD},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(response.data["error"]["code"], "infeasible_route")
        self.assertIn("Server-Timing", response)
        header = response["Server-Timing"]
        self.assertIn("route", header)
        self.assertIn("corridor", header)
