"""Tests for the Static Images map_url builder.

`build_map_url` is a pure string builder -- no `requests` call is ever
made (the backend never fetches the PNG), so these tests exercise the
function directly against a synthetic `Route`.
"""
from decimal import Decimal

from django.test import SimpleTestCase, override_settings
from shapely.geometry import LineString

from routing.map_url import MAX_URL_LENGTH, STATIC_IMAGES_URL, build_map_url
from routing.services.mapbox import Route

START = (Decimal("41.8781"), Decimal("-87.6298"))
FINISH = (Decimal("38.6270"), Decimal("-90.1994"))
STOP_COORDS = [
    (Decimal("40.4842"), Decimal("-88.5588")),
    (Decimal("39.2136"), Decimal("-89.6501")),
]

ROUTE_COORDS = [
    [-87.6298, 41.8781],
    [-87.9073, 41.5250],
    [-88.2434, 41.1306],
    [-88.5588, 40.4842],
    [-89.3985, 39.7817],
    [-89.6501, 39.2136],
    [-90.0715, 38.8026],
    [-90.1994, 38.6270],
]


def _route(coords=None):
    coords = coords if coords is not None else ROUTE_COORDS
    return Route(
        total_route_mi=Decimal("300"),
        geometry=LineString(coords),
        raw_coordinates=coords,
    )


@override_settings(MAPBOX_TOKEN="test-token")
class BuildMapUrlHappyPathTests(SimpleTestCase):
    """Start pin, finish pin, one pin per stop, auto viewport,
    a path- overlay, all under the URL limit."""

    def test_url_starts_with_static_images_base(self):
        url = build_map_url(_route(), START, FINISH, STOP_COORDS)

        self.assertTrue(url.startswith(STATIC_IMAGES_URL))

    def test_url_contains_auto_viewport_and_path_overlay(self):
        url = build_map_url(_route(), START, FINISH, STOP_COORDS)

        self.assertIn("/auto/", url)
        self.assertIn("path-", url)

    def test_url_contains_exactly_one_start_and_finish_pin(self):
        url = build_map_url(_route(), START, FINISH, STOP_COORDS)

        self.assertEqual(url.count("pin-s-a+"), 1)
        self.assertEqual(url.count("pin-s-b+"), 1)

    def test_url_contains_one_pin_per_stop(self):
        url = build_map_url(_route(), START, FINISH, STOP_COORDS)

        for n in range(1, len(STOP_COORDS) + 1):
            self.assertIn(f"pin-s-{n}+", url)

    def test_url_stays_under_max_length(self):
        url = build_map_url(_route(), START, FINISH, STOP_COORDS)

        self.assertLessEqual(len(url), MAX_URL_LENGTH)

    def test_no_stops_still_produces_start_and_finish_pins_only(self):
        url = build_map_url(_route(), START, FINISH, [])

        self.assertEqual(url.count("pin-s-a+"), 1)
        self.assertEqual(url.count("pin-s-b+"), 1)
        self.assertNotIn("pin-s-1+", url)


@override_settings(MAPBOX_TOKEN="test-token")
class BuildMapUrlDenseGeometryTests(SimpleTestCase):
    """The guard loop progressively simplifies a dense route until
    the encoded URL fits under MAX_URL_LENGTH, rather than emitting an
    over-limit URL."""

    def test_guard_loop_keeps_dense_route_under_limit(self):
        dense_coords = [
            [
                -87.6298 + (i * 0.0009),
                41.8781 - (i * 0.0006) + (0.02 * ((-1) ** i)),
            ]
            for i in range(3000)
        ]

        url = build_map_url(_route(dense_coords), START, FINISH, STOP_COORDS)

        self.assertLessEqual(len(url), MAX_URL_LENGTH)
        self.assertTrue(url.startswith(STATIC_IMAGES_URL))
        self.assertIn("path-", url)


@override_settings(MAPBOX_TOKEN="test-token")
class BuildMapUrlPolylineEncodingTests(SimpleTestCase):
    """The encoded polyline is an opaque payload that can contain
    URL-unsafe characters (`\\`, `|`, `?`, ...). Mapbox's Static Images
    endpoint requires this payload to be percent-encoded, or a browser
    silently corrupts the path (`\\` -> `/`) or truncates the query
    string early (`?`), producing a "Not Authorized" response even with
    a valid token."""

    # Coordinates whose encoded polyline is known to contain a raw
    # backslash and question mark (`??~`f@f{\\`), confirmed via
    # `polyline.encode`.
    BACKSLASH_COORDS = [[0.0, 0.0], [-0.153, -0.2]]

    def test_unsafe_polyline_chars_are_percent_encoded_in_path(self):
        url = build_map_url(
            _route(self.BACKSLASH_COORDS), START, FINISH, []
        )

        path_segment = url.split("?access_token=")[0]

        # The raw unsafe characters must never appear in the URL path --
        # a raw `\` gets rewritten to `/` by browsers, corrupting the
        # `/static/{overlay}/auto/{size}` path, and a raw `?` would start
        # the query string early and orphan the real access_token param.
        self.assertNotIn("\\", path_segment)
        self.assertNotIn("?", path_segment)

        # The percent-encoded forms must be present instead.
        self.assertIn("%5C", url)
        self.assertIn("%3F", url)

    def test_access_token_param_survives_unsafe_polyline_chars(self):
        url = build_map_url(
            _route(self.BACKSLASH_COORDS), START, FINISH, []
        )

        self.assertTrue(url.endswith("access_token=test-token"))
        self.assertEqual(url.count("access_token="), 1)


@override_settings(MAPBOX_TOKEN="test-token")
class BuildMapUrlTokenSafetyTests(SimpleTestCase):
    """The access token rides only in the access_token query param, never
    inside the marker/path overlay segment."""

    def test_token_only_appears_after_access_token_param(self):
        url = build_map_url(_route(), START, FINISH, STOP_COORDS)

        self.assertIn("?access_token=test-token", url)
        overlay_segment = url.split("?access_token=")[0]
        self.assertNotIn("test-token", overlay_segment)
        self.assertTrue(url.endswith("access_token=test-token"))
