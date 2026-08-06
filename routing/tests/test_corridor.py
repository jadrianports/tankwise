"""Tests for the corridor filter.

Routes are constructed directly as synthetic `Route` objects -- no
Mapbox call is ever made. All tests touch the DB (seeded `Station`
rows), so they use `django.test.TestCase`.
"""
import math
from decimal import Decimal
from unittest import skipUnless

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from shapely.geometry import LineString

from routing.models import GeocodePrecision, GeocodeStatus, Station
from routing.services.corridor import IndexedStation, candidates, reset_index
from routing.services.mapbox import Route


class CorridorTestCase(TestCase):
    """Shared base: resets the process-level STRtree index before every
    test so no test can inherit a stale tree built from a previous test's
    (rolled-back) Station rows -- the module global survives Django's
    per-test transaction rollback, so this is the only correct place to
    invalidate it."""

    def setUp(self):
        super().setUp()
        reset_index()


def _make_station(
    opis_id,
    geocode_status,
    latitude=None,
    longitude=None,
    geocode_precision=None,
):
    """Extends test_models.py's `_make_station` idiom with a
    `geocode_precision=` kwarg needed for the geocode-precision tiering cases."""
    return Station.objects.create(
        opis_id=opis_id,
        name="Test Station",
        address="I-00, EXIT 1 & US-1",
        city="Anytown",
        state="OK",
        rack_id="100",
        retail_price=Decimal("3.259"),
        geocode_status=geocode_status,
        geocode_precision=geocode_precision,
        latitude=latitude,
        longitude=longitude,
        observation_count=1,
        price_min=Decimal("3.259"),
        price_max=Decimal("3.259"),
    )


class CorridorCurveInclusionTests(CorridorTestCase):
    """A station near the middle of a
    curving route is included even though it is far from the straight
    start-finish chord; a station near that chord but far from the
    actual road is excluded. The L-shaped route below is authored so a
    naive endpoint- or chord-distance filter gets both calls backwards:
    the mid-curve point sits ~34.6 mi from the nearest
    route endpoint and ~26.8 mi from the A-C chord (both > the 20-mi
    city width), yet it is exactly ON the real two-segment road
    (perpendicular distance 0); the near-chord point sits exactly on
    the A-C chord (distance ~0) yet is ~34.6 mi from the real road.
    """

    ROUTE_COORDS = [
        (-95.00, 35.00),
        (-95.00, 36.00),
        (-93.50, 36.00),
    ]

    def _route(self):
        return Route(
            total_route_mi=Decimal("153.5"),
            geometry=LineString(self.ROUTE_COORDS),
            raw_coordinates=self.ROUTE_COORDS,
        )

    def test_mid_curve_on_road_station_is_included(self):
        station = _make_station(
            opis_id=101,
            geocode_status=GeocodeStatus.OK,
            latitude=Decimal("35.50"),
            longitude=Decimal("-95.00"),
            geocode_precision=GeocodePrecision.ROOFTOP,
        )

        result = candidates(self._route())

        self.assertIn(station.opis_id, {c.opis_id for c in result})

    def test_near_chord_off_road_station_is_excluded(self):
        _make_station(
            opis_id=102,
            geocode_status=GeocodeStatus.OK,
            latitude=Decimal("35.50"),
            longitude=Decimal("-94.25"),
            geocode_precision=GeocodePrecision.CITY,
        )

        result = candidates(self._route())

        self.assertEqual(result, [])


class CorridorPositioningTests(CorridorTestCase):
    """distance_from_start_mi is the project()/length fraction
    times the route's own total_route_mi, within tolerance, and always
    lies in [0, total_route_mi]."""

    ROUTE_COORDS = [(-97.00, 30.00), (-97.00, 40.00)]
    TOTAL_ROUTE_MI = Decimal("700")

    def _route(self):
        return Route(
            total_route_mi=self.TOTAL_ROUTE_MI,
            geometry=LineString(self.ROUTE_COORDS),
            raw_coordinates=self.ROUTE_COORDS,
        )

    def test_station_at_known_fraction_positions_within_tolerance(self):
        # 32.50 is exactly 25% of the way from 30.00 to 40.00.
        _make_station(
            opis_id=201,
            geocode_status=GeocodeStatus.OK,
            latitude=Decimal("32.50"),
            longitude=Decimal("-97.00"),
            geocode_precision=GeocodePrecision.ROOFTOP,
        )

        result = candidates(self._route())

        self.assertEqual(len(result), 1)
        expected = self.TOTAL_ROUTE_MI * Decimal("0.25")
        self.assertAlmostEqual(
            float(result[0].distance_from_start_mi), float(expected), delta=1.0
        )
        self.assertGreaterEqual(result[0].distance_from_start_mi, Decimal("0"))
        self.assertLessEqual(result[0].distance_from_start_mi, self.TOTAL_ROUTE_MI)


class CorridorSimplifyLengthInvariantTests(CorridorTestCase):
    """`route_length_mi` must be derived from the SAME simplified planar
    line the vectorized `.distance()`/`.project()` calls run against --
    never from a stale full-resolution length. If `route_length_mi` were
    ever taken from the pre-simplification line instead, a known-fraction
    station's `distance_from_start_mi` would skew measurably -- this test
    is meant to catch that class of bug.

    A PERFECTLY STRAIGHT densified route (all points collinear) is NOT a
    sufficient fixture here, even though `.simplify()` measurably drops its
    vertex count: for a straight line, the sum of consecutive segment
    lengths always equals the straight-line distance exactly (no chord ever
    "cuts a corner"), so the pre- and post-simplification lengths are
    bit-for-bit identical and the hazard this test exists to catch cannot
    manifest -- confirmed empirically: swapping `route_length_mi`'s source
    to the pre-simplification line on a collinear fixture produced zero
    deviation. This fixture instead reuses the technique proven in
    `MultiLegSimplifyLengthInvariantTests`
    (`routing/tests/test_multi_leg.py`): a gentle high-frequency sine
    wiggle (amplitude comfortably under `SIMPLIFY_TOLERANCE_MI`'s 0.05 mi
    threshold) around the north-south trend line -- real curvature that
    `.simplify()` genuinely shortens, so a stale pre-simplification length
    measurably disagrees with the correct one."""

    TOTAL_ROUTE_MI = Decimal("700")
    # Higher point/cycle count than the multi-leg analogue's per-leg
    # fixture -- this route spans a wider 10-degree latitude range (vs.
    # ~5 degrees per multi-leg leg), so matching the multi-leg fixture's
    # cycles-per-degree density (and then some, for comfortable margin
    # above this test's delta=1.0 tolerance) takes proportionally more
    # points and cycles. Tuned empirically via this task's manual
    # sabotage/revert check (see class docstring).
    N_POINTS = 10000
    WIGGLE_AMPLITUDE_DEG = 0.0009
    WIGGLE_CYCLES = 1200

    def _densified_route_coords(self, n_points=None):
        # A wiggly north-south line: points spaced closely enough, and
        # curved enough, that .simplify(0.05) both collapses the vast
        # majority of vertices AND genuinely shortens the line -- verified
        # below via the vertex-count assertion and, separately, via this
        # task's manual sabotage/revert check.
        n = n_points if n_points is not None else self.N_POINTS
        coords = []
        for i in range(n):
            t = i / (n - 1)
            lat = 30.00 + (40.00 - 30.00) * t
            lng = -97.00 + self.WIGGLE_AMPLITUDE_DEG * math.sin(
                2 * math.pi * self.WIGGLE_CYCLES * t
            )
            coords.append((lng, lat))
        return coords

    def _route(self):
        coords = self._densified_route_coords()
        return Route(
            total_route_mi=self.TOTAL_ROUTE_MI,
            geometry=LineString(coords),
            raw_coordinates=coords,
        )

    def test_simplification_reduces_vertex_count_on_this_fixture(self):
        """Sanity check that this test fixture actually exercises
        simplification -- otherwise the invariant test below would pass
        vacuously."""
        from routing.services.corridor import (
            SIMPLIFY_TOLERANCE_MI,
            build_planar_route,
            mean_lat_rad,
        )

        coords = self._densified_route_coords()
        mean_lat = mean_lat_rad(coords)
        planar_route = build_planar_route(coords, mean_lat=mean_lat)
        simplified = planar_route.simplify(SIMPLIFY_TOLERANCE_MI)

        self.assertLess(len(simplified.coords), len(planar_route.coords))

    def test_known_fraction_station_positions_correctly_on_simplified_route(self):
        # 32.50 is exactly 25% of the way from 30.00 to 40.00. The wiggle
        # only perturbs longitude by up to 0.0009 deg (well inside both
        # corridor tiers), so this station still lands inside the corridor
        # of every wiggled coordinate near it.
        _make_station(
            opis_id=901,
            geocode_status=GeocodeStatus.OK,
            latitude=Decimal("32.50"),
            longitude=Decimal("-97.00"),
            geocode_precision=GeocodePrecision.ROOFTOP,
        )

        result = candidates(self._route())

        self.assertEqual(len(result), 1)
        expected = self.TOTAL_ROUTE_MI * Decimal("0.25")
        self.assertAlmostEqual(
            float(result[0].distance_from_start_mi), float(expected), delta=1.0
        )


class CorridorPrecisionTieringTests(CorridorTestCase):
    """A station ~10 mi off the route is excluded at the 5-mi
    rooftop tier but included at the 20-mi city tier."""

    ROUTE_COORDS = [(-97.00, 30.00), (-97.00, 40.00)]

    def _route(self):
        return Route(
            total_route_mi=Decimal("700"),
            geometry=LineString(self.ROUTE_COORDS),
            raw_coordinates=self.ROUTE_COORDS,
        )

    @override_settings(CORRIDOR_ROOFTOP_MI="5", CORRIDOR_CITY_MI="20")
    def test_rooftop_precision_excluded_but_city_precision_included(self):
        # ~10 mi east of the route at the corridor's mean latitude.
        offset_lng = Decimal("-96.8235")
        rooftop_station = _make_station(
            opis_id=301,
            geocode_status=GeocodeStatus.OK,
            latitude=Decimal("35.00"),
            longitude=offset_lng,
            geocode_precision=GeocodePrecision.ROOFTOP,
        )
        city_station = _make_station(
            opis_id=302,
            geocode_status=GeocodeStatus.OK,
            latitude=Decimal("35.00"),
            longitude=offset_lng,
            geocode_precision=GeocodePrecision.CITY,
        )

        result_ids = {c.opis_id for c in candidates(self._route())}

        self.assertNotIn(rooftop_station.opis_id, result_ids)
        self.assertIn(city_station.opis_id, result_ids)


class CorridorRoutableEnforcementTests(CorridorTestCase):
    """A failed/pending station inside the bbox -- even
    directly on the route -- must never become a candidate; only
    Station.objects.routable() rows are eligible."""

    ROUTE_COORDS = [(-97.00, 30.00), (-97.00, 40.00)]

    def _route(self):
        return Route(
            total_route_mi=Decimal("700"),
            geometry=LineString(self.ROUTE_COORDS),
            raw_coordinates=self.ROUTE_COORDS,
        )

    def test_failed_station_on_route_is_never_a_candidate(self):
        _make_station(
            opis_id=401,
            geocode_status=GeocodeStatus.FAILED,
            latitude=Decimal("35.00"),
            longitude=Decimal("-97.00"),
            geocode_precision=None,
        )

        self.assertEqual(candidates(self._route()), [])

    def test_pending_station_on_route_is_never_a_candidate(self):
        _make_station(
            opis_id=402,
            geocode_status=GeocodeStatus.PENDING,
            latitude=Decimal("35.00"),
            longitude=Decimal("-97.00"),
            geocode_precision=None,
        )

        self.assertEqual(candidates(self._route()), [])


class CorridorQueryCountTests(CorridorTestCase):
    """The bbox prefilter is exactly one query, no
    N+1, regardless of how many stations are seeded."""

    ROUTE_COORDS = [(-97.00, 30.00), (-97.00, 40.00)]

    def _route(self):
        return Route(
            total_route_mi=Decimal("700"),
            geometry=LineString(self.ROUTE_COORDS),
            raw_coordinates=self.ROUTE_COORDS,
        )

    def test_candidates_runs_exactly_one_query(self):
        for i in range(5):
            _make_station(
                opis_id=500 + i,
                geocode_status=GeocodeStatus.OK,
                latitude=Decimal("3" + f"{i}.00"),
                longitude=Decimal("-97.00"),
                geocode_precision=GeocodePrecision.ROOFTOP,
            )

        with self.assertNumQueries(1):
            candidates(self._route())


class CorridorIndexUsageTest(CorridorTestCase):
    """assertNumQueries proves count, not query
    plan -- supplement with an EXPLAIN QUERY PLAN assertion that the
    bbox prefilter hits the (latitude, longitude) composite index
    rather than a full table scan."""

    @skipUnless(
        connection.vendor == "sqlite",
        "EXPLAIN QUERY PLAN and its SEARCH/SCAN vocabulary are SQLite-only; "
        "Postgres reports an entirely different plan format and would pick a "
        "sequential scan on a table this small regardless of the index.",
    )
    def test_bbox_prefilter_uses_index_not_full_scan(self):
        qs = Station.objects.routable().filter(
            latitude__range=(Decimal("30"), Decimal("40")),
            longitude__range=(Decimal("-100"), Decimal("-90")),
        )
        sql, params = qs.query.sql_with_params()
        with connection.cursor() as cursor:
            cursor.execute(f"EXPLAIN QUERY PLAN {sql}", params)
            plan = cursor.fetchall()
        plan_text = " ".join(str(row) for row in plan)

        self.assertIn("SEARCH", plan_text)
        self.assertNotIn("SCAN TABLE routing_station", plan_text)
        self.assertNotIn("SCAN routing_station", plan_text)


class StrtreeIndexTests(CorridorTestCase):
    """The STRtree index: explicit invalidation, a DB-free second call,
    the high-latitude buffer-anisotropy safety margin, and the
    empty-table edge case."""

    ROUTE_COORDS = [(-97.00, 30.00), (-97.00, 40.00)]

    def _route(self):
        return Route(
            total_route_mi=Decimal("700"),
            geometry=LineString(self.ROUTE_COORDS),
            raw_coordinates=self.ROUTE_COORDS,
        )

    def test_station_created_after_index_build_is_invisible_until_reset(self):
        # Build the index against an empty table.
        self.assertEqual(candidates(self._route()), [])

        station = _make_station(
            opis_id=601,
            geocode_status=GeocodeStatus.OK,
            latitude=Decimal("35.00"),
            longitude=Decimal("-97.00"),
            geocode_precision=GeocodePrecision.ROOFTOP,
        )

        # Stale index: the new row is invisible until reset_index() runs.
        self.assertEqual(candidates(self._route()), [])

        reset_index()

        result_ids = {c.opis_id for c in candidates(self._route())}
        self.assertIn(station.opis_id, result_ids)

    def test_second_candidates_call_issues_zero_queries(self):
        _make_station(
            opis_id=602,
            geocode_status=GeocodeStatus.OK,
            latitude=Decimal("35.00"),
            longitude=Decimal("-97.00"),
            geocode_precision=GeocodePrecision.ROOFTOP,
        )

        candidates(self._route())  # first call: builds the index (1 query)

        with CaptureQueriesContext(connection) as ctx:
            candidates(self._route())

        self.assertEqual(len(ctx.captured_queries), 0)

    def test_high_latitude_north_south_route_is_not_under_buffered(self):
        """A station genuinely within the 20-mi city corridor of a
        north-south route at ~47 deg latitude must survive the STRtree
        buffer query. At this latitude cos(lat) ~= 0.68, so the
        longitude-axis pad is meaningfully larger than the latitude-axis
        pad; buffering by anything smaller than the larger pad would
        under-include and silently drop this station before the precise
        perpendicular test ever runs."""
        route = Route(
            total_route_mi=Decimal("276"),
            geometry=LineString([(-97.00, 45.00), (-97.00, 49.00)]),
            raw_coordinates=[(-97.00, 45.00), (-97.00, 49.00)],
        )
        # ~18 real miles east of the route at the route's mean latitude
        # (47 deg) -- within the 20-mi city tier, but farther east in
        # degrees than the (smaller) latitude-axis pad would allow.
        station = _make_station(
            opis_id=603,
            geocode_status=GeocodeStatus.OK,
            latitude=Decimal("47.00"),
            longitude=Decimal("-97.00") + Decimal("0.3815564873288794405526510175"),
            geocode_precision=GeocodePrecision.CITY,
        )

        result_ids = {c.opis_id for c in candidates(route)}

        self.assertIn(station.opis_id, result_ids)

    def test_empty_station_table_returns_empty_list_without_raising(self):
        self.assertEqual(candidates(self._route()), [])


class IndexedStationAttributeNamesTest(CorridorTestCase):
    """Pins the eight attribute names the corridor path relies on
    (`station.name` / `station.retail_price` / `station.geocode_precision`
    etc.) so a future field rename on `IndexedStation` fails loudly here
    rather than silently degrading candidate selection downstream."""

    def test_indexed_station_has_the_eight_relied_upon_fields(self):
        self.assertEqual(
            IndexedStation._fields,
            (
                "opis_id",
                "name",
                "state",
                "retail_price",
                "latitude",
                "longitude",
                "geocode_precision",
                "price_source",
            ),
        )


class ZeroLengthRouteTests(CorridorTestCase):
    """A start and finish resolving to the same point yields a zero-length
    route. The along-line fraction is a 0/0 `DivisionUndefined` there, which
    is not part of the domain-exception hierarchy `custom_exception_handler`
    maps -- so before this guard the request surfaced as a bare HTTP 500
    rather than a clean 400. `_candidates_multi_leg` already guarded its own
    per-leg equivalent; this pins the single-leg path to the same behavior."""

    def _degenerate_route(self):
        point = [-97.0, 35.0]
        return Route(
            total_route_mi=Decimal("0"),
            geometry=LineString([point, point]),
            raw_coordinates=[point, point],
            duration_s=Decimal("0"),
            annotation_durations=[],
            annotation_distances=[],
            leg_distances_mi=[Decimal("0")],
            leg_annotation_lengths=[0],
        )

    def test_zero_length_route_returns_no_candidates_instead_of_raising(self):
        _make_station(
            1,
            GeocodeStatus.OK,
            latitude=Decimal("35.0"),
            longitude=Decimal("-97.0"),
            geocode_precision=GeocodePrecision.ROOFTOP,
        )

        self.assertEqual(candidates(self._degenerate_route()), [])

    def test_zero_length_route_reaches_the_solver_s_own_input_guard(self):
        """Returning [] must hand the degenerate route on to the solver,
        whose `_validate` raises the properly-mapped
        `InvalidRouteInputError` (-> HTTP 400)."""
        from routing.services.exceptions import InvalidRouteInputError
        from routing.services.solver import solve

        route = self._degenerate_route()
        cands = candidates(route)

        with self.assertRaises(InvalidRouteInputError):
            # D-05: untimed -- this asserts _validate's own input guard,
            # never reached far enough to time-box anything.
            solve(cands, route.total_route_mi, deadline=None)
