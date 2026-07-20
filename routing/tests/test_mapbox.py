"""Tests for the Mapbox Directions client.

The transport boundary (`routing.services.mapbox._SESSION.get`) is always
mocked -- no live network call is ever performed. The response parser is
exercised against a recorded-shape fixture reproducing Mapbox's verified
Directions v5 "Ok" response.
"""
import json
from decimal import Decimal
from pathlib import Path
from unittest import mock

import requests
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings
from shapely.geometry import LineString

from routing.services.mapbox import (
    MapboxRequestError,
    Route,
    RouteNotFoundError,
    geocode,
    get_routes,
)

FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "mapbox_directions_response.json"
)
with open(FIXTURE_PATH, encoding="utf-8") as f:
    FIXTURE = json.load(f)

MULTI_ROUTE_FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "mapbox_directions_response_multi.json"
)
with open(MULTI_ROUTE_FIXTURE_PATH, encoding="utf-8") as f:
    MULTI_ROUTE_FIXTURE = json.load(f)

NO_ANNOTATION_FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "mapbox_directions_response_no_annotation.json"
)
with open(NO_ANNOTATION_FIXTURE_PATH, encoding="utf-8") as f:
    NO_ANNOTATION_FIXTURE = json.load(f)

GEOCODING_FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "mapbox_geocoding_response.json"
)
with open(GEOCODING_FIXTURE_PATH, encoding="utf-8") as f:
    GEOCODING_FIXTURE = json.load(f)

START = (Decimal("41.8781"), Decimal("-87.6298"))
FINISH = (Decimal("38.6270"), Decimal("-90.1994"))
ADDRESS = "401 N Michigan Ave, Chicago, IL"


class _StubResponse:
    """Minimal stand-in for a `requests.Response`."""

    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else FIXTURE

    def json(self):
        return self._payload


@override_settings(MAPBOX_TOKEN="test-token")
class GetRouteHappyPathTests(SimpleTestCase):
    """get_routes()[0] resolves a typed Route in exactly one call."""

    def test_returns_route_with_exactly_one_call(self):
        with mock.patch(
            "routing.services.mapbox._SESSION.get", return_value=_StubResponse()
        ) as mock_get:
            route = get_routes(START, FINISH)[0]

        mock_get.assert_called_once()
        self.assertIsInstance(route, Route)

    def test_total_route_mi_is_decimal_derived_from_meters(self):
        with mock.patch(
            "routing.services.mapbox._SESSION.get", return_value=_StubResponse()
        ):
            route = get_routes(START, FINISH)[0]

        expected = Decimal(str(FIXTURE["routes"][0]["distance"])) / Decimal(
            "1609.344"
        )
        self.assertEqual(route.total_route_mi, expected)
        self.assertIsInstance(route.total_route_mi, Decimal)

    def test_geometry_is_linestring_with_matching_coord_count(self):
        with mock.patch(
            "routing.services.mapbox._SESSION.get", return_value=_StubResponse()
        ):
            route = get_routes(START, FINISH)[0]

        fixture_coords = FIXTURE["routes"][0]["geometry"]["coordinates"]
        self.assertIsInstance(route.geometry, LineString)
        self.assertEqual(len(route.geometry.coords), len(fixture_coords))

    def test_raw_coordinates_equal_fixture_coordinates(self):
        with mock.patch(
            "routing.services.mapbox._SESSION.get", return_value=_StubResponse()
        ):
            route = get_routes(START, FINISH)[0]

        fixture_coords = FIXTURE["routes"][0]["geometry"]["coordinates"]
        self.assertEqual(route.raw_coordinates, fixture_coords)


@override_settings(MAPBOX_TOKEN="test-token")
class DirectionsRequestParamsTests(SimpleTestCase):
    """The outbound request asks for alternatives and annotations
    alongside the existing overview/geometries params, all via `params`
    (never interpolated into the URL)."""

    def test_params_include_alternatives_annotations_overview_geometries(self):
        with mock.patch(
            "routing.services.mapbox._SESSION.get", return_value=_StubResponse()
        ) as mock_get:
            get_routes(START, FINISH)[0]

        _, kwargs = mock_get.call_args
        params = kwargs["params"]
        self.assertEqual(params["alternatives"], "true")
        self.assertEqual(params["annotations"], "duration,distance")
        self.assertEqual(params["overview"], "full")
        self.assertEqual(params["geometries"], "geojson")

    def test_get_route_still_issues_exactly_one_call(self):
        with mock.patch(
            "routing.services.mapbox._SESSION.get", return_value=_StubResponse()
        ) as mock_get:
            get_routes(START, FINISH)[0]

        mock_get.assert_called_once()


@override_settings(MAPBOX_TOKEN="test-token")
class GetRoutesMultiAlternativeTests(SimpleTestCase):
    """get_routes() parses every alternative Mapbox returns, in order,
    each carrying its own duration and index-aligned annotation arrays."""

    def test_returns_list_of_three_routes_in_order(self):
        with mock.patch(
            "routing.services.mapbox._SESSION.get",
            return_value=_StubResponse(payload=MULTI_ROUTE_FIXTURE),
        ):
            routes = get_routes(START, FINISH)

        self.assertEqual(len(routes), 3)
        for route in routes:
            self.assertIsInstance(route, Route)

    def test_alternative_index_matches_position(self):
        with mock.patch(
            "routing.services.mapbox._SESSION.get",
            return_value=_StubResponse(payload=MULTI_ROUTE_FIXTURE),
        ):
            routes = get_routes(START, FINISH)

        self.assertEqual([r.alternative_index for r in routes], [0, 1, 2])

    def test_duration_s_matches_fixture_as_exact_decimal(self):
        with mock.patch(
            "routing.services.mapbox._SESSION.get",
            return_value=_StubResponse(payload=MULTI_ROUTE_FIXTURE),
        ):
            routes = get_routes(START, FINISH)

        for route, fixture_route in zip(routes, MULTI_ROUTE_FIXTURE["routes"]):
            expected = Decimal(str(fixture_route["duration"]))
            self.assertEqual(route.duration_s, expected)
            self.assertIsInstance(route.duration_s, Decimal)

    def test_annotation_arrays_length_matches_coordinates_minus_one(self):
        with mock.patch(
            "routing.services.mapbox._SESSION.get",
            return_value=_StubResponse(payload=MULTI_ROUTE_FIXTURE),
        ):
            routes = get_routes(START, FINISH)

        for route in routes:
            expected_len = len(route.raw_coordinates) - 1
            self.assertEqual(len(route.annotation_durations), expected_len)
            self.assertEqual(len(route.annotation_distances), expected_len)

    def test_annotation_distances_in_miles_sum_to_approximately_total(self):
        with mock.patch(
            "routing.services.mapbox._SESSION.get",
            return_value=_StubResponse(payload=MULTI_ROUTE_FIXTURE),
        ):
            routes = get_routes(START, FINISH)

        for route in routes:
            summed = sum(route.annotation_distances)
            self.assertAlmostEqual(
                float(summed), float(route.total_route_mi), places=3
            )


@override_settings(MAPBOX_TOKEN="test-token")
class NoAnnotationFallbackTests(SimpleTestCase):
    """A well-formed response whose routes carry no `annotation` key
    parses without raising, leaving the annotation lists empty."""

    def test_parses_without_raising_with_empty_annotation_lists(self):
        with mock.patch(
            "routing.services.mapbox._SESSION.get",
            return_value=_StubResponse(payload=NO_ANNOTATION_FIXTURE),
        ):
            routes = get_routes(START, FINISH)

        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].annotation_durations, [])
        self.assertEqual(routes[0].annotation_distances, [])


@override_settings(MAPBOX_TOKEN="test-token")
class TokenHandlingTests(SimpleTestCase):
    """The access token rides in params, never the URL string."""

    def test_token_in_params_not_in_url(self):
        with mock.patch(
            "routing.services.mapbox._SESSION.get", return_value=_StubResponse()
        ) as mock_get:
            get_routes(START, FINISH)[0]

        args, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"]["access_token"], "test-token")
        self.assertNotIn("test-token", args[0])


@override_settings(MAPBOX_TOKEN="test-token")
class RouteNotFoundTests(SimpleTestCase):
    """code != 'Ok' or an empty routes list raises RouteNotFoundError."""

    def test_no_route_code_raises_route_not_found(self):
        no_route_payload = {
            "code": "NoRoute",
            "routes": [],
            "waypoints": [],
            "uuid": "x",
        }
        with mock.patch(
            "routing.services.mapbox._SESSION.get",
            return_value=_StubResponse(payload=no_route_payload),
        ):
            with self.assertRaises(RouteNotFoundError):
                get_routes(START, FINISH)[0]

    def test_ok_code_with_empty_routes_raises_route_not_found(self):
        empty_routes_payload = dict(FIXTURE)
        empty_routes_payload["routes"] = []
        with mock.patch(
            "routing.services.mapbox._SESSION.get",
            return_value=_StubResponse(payload=empty_routes_payload),
        ):
            with self.assertRaises(RouteNotFoundError):
                get_routes(START, FINISH)[0]


@override_settings(MAPBOX_TOKEN="test-token")
class MapboxRequestErrorTests(SimpleTestCase):
    """A non-200 status or a requests transport failure raises
    MapboxRequestError; neither raised message contains the token."""

    def test_non_200_status_raises_mapbox_request_error(self):
        with mock.patch(
            "routing.services.mapbox._SESSION.get",
            return_value=_StubResponse(status_code=500),
        ):
            with self.assertRaises(MapboxRequestError) as ctx:
                get_routes(START, FINISH)[0]

        self.assertNotIn("test-token", str(ctx.exception))

    def test_request_exception_raises_mapbox_request_error(self):
        with mock.patch(
            "routing.services.mapbox._SESSION.get",
            side_effect=requests.RequestException("boom"),
        ):
            with self.assertRaises(MapboxRequestError) as ctx:
                get_routes(START, FINISH)[0]

        self.assertNotIn("test-token", str(ctx.exception))


class MissingTokenTests(SimpleTestCase):
    """An unset MAPBOX_TOKEN raises ImproperlyConfigured before any HTTP
    call is attempted."""

    @override_settings(MAPBOX_TOKEN=None)
    def test_missing_token_raises_before_any_http_call(self):
        with mock.patch("routing.services.mapbox._SESSION.get") as mock_get:
            with self.assertRaises(ImproperlyConfigured):
                get_routes(START, FINISH)[0]

        mock_get.assert_not_called()


@override_settings(MAPBOX_TOKEN="test-token")
class GeocodeHappyPathTests(SimpleTestCase):
    """geocode resolves an address to a (lat, lng) Decimal
    pair in exactly one Mapbox Geocoding v6 forward call."""

    def test_returns_lat_lng_decimal_pair_with_exactly_one_call(self):
        with mock.patch(
            "routing.services.mapbox._SESSION.get",
            return_value=_StubResponse(payload=GEOCODING_FIXTURE),
        ) as mock_get:
            lat, lng = geocode(ADDRESS)

        mock_get.assert_called_once()
        fixture_lng, fixture_lat = GEOCODING_FIXTURE["features"][0]["geometry"][
            "coordinates"
        ]
        self.assertIsInstance(lat, Decimal)
        self.assertIsInstance(lng, Decimal)
        self.assertEqual(lat, Decimal(str(fixture_lat)))
        self.assertEqual(lng, Decimal(str(fixture_lng)))

    def test_request_params_include_country_us_and_limit_one(self):
        with mock.patch(
            "routing.services.mapbox._SESSION.get",
            return_value=_StubResponse(payload=GEOCODING_FIXTURE),
        ) as mock_get:
            geocode(ADDRESS)

        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"]["country"], "us")
        self.assertEqual(kwargs["params"]["limit"], 1)
        self.assertEqual(kwargs["params"]["q"], ADDRESS)


@override_settings(MAPBOX_TOKEN="test-token")
class GeocodeTokenHandlingTests(SimpleTestCase):
    """The access token rides in params, never the URL string."""

    def test_token_in_params_not_in_url(self):
        with mock.patch(
            "routing.services.mapbox._SESSION.get",
            return_value=_StubResponse(payload=GEOCODING_FIXTURE),
        ) as mock_get:
            geocode(ADDRESS)

        args, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"]["access_token"], "test-token")
        self.assertNotIn("test-token", args[0])


class GeocodeMissingTokenTests(SimpleTestCase):
    """An unset MAPBOX_TOKEN raises ImproperlyConfigured before any HTTP
    call is attempted."""

    @override_settings(MAPBOX_TOKEN=None)
    def test_missing_token_raises_before_any_http_call(self):
        with mock.patch("routing.services.mapbox._SESSION.get") as mock_get:
            with self.assertRaises(ImproperlyConfigured):
                geocode(ADDRESS)

        mock_get.assert_not_called()


@override_settings(MAPBOX_TOKEN="test-token")
class GeocodeRequestErrorTests(SimpleTestCase):
    """A non-200 status or a requests transport failure raises
    MapboxRequestError; neither raised message contains the token."""

    def test_non_200_status_raises_mapbox_request_error(self):
        with mock.patch(
            "routing.services.mapbox._SESSION.get",
            return_value=_StubResponse(status_code=500, payload=GEOCODING_FIXTURE),
        ):
            with self.assertRaises(MapboxRequestError) as ctx:
                geocode(ADDRESS)

        self.assertNotIn("test-token", str(ctx.exception))

    def test_request_exception_raises_mapbox_request_error(self):
        with mock.patch(
            "routing.services.mapbox._SESSION.get",
            side_effect=requests.RequestException("boom"),
        ):
            with self.assertRaises(MapboxRequestError) as ctx:
                geocode(ADDRESS)

        self.assertNotIn("test-token", str(ctx.exception))


@override_settings(MAPBOX_TOKEN="test-token")
class GeocodeNoResultTests(SimpleTestCase):
    """An empty features list raises RouteNotFoundError."""

    def test_empty_features_raises_route_not_found(self):
        empty_payload = {"type": "FeatureCollection", "features": []}
        with mock.patch(
            "routing.services.mapbox._SESSION.get",
            return_value=_StubResponse(payload=empty_payload),
        ):
            with self.assertRaises(RouteNotFoundError):
                geocode(ADDRESS)
