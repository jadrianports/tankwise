"""Tests for the corridor filter.

Routes are constructed directly as synthetic `Route` objects -- no
Mapbox call is ever made. All tests touch the DB (seeded `Station`
rows), so they use `django.test.TestCase`.
"""
from decimal import Decimal

from django.db import connection
from django.test import TestCase, override_settings
from shapely.geometry import LineString

from routing.models import GeocodePrecision, GeocodeStatus, Station
from routing.services.corridor import candidates
from routing.services.mapbox import Route


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


class CorridorCurveInclusionTests(TestCase):
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


class CorridorPositioningTests(TestCase):
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


class CorridorPrecisionTieringTests(TestCase):
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


class CorridorRoutableEnforcementTests(TestCase):
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


class CorridorQueryCountTests(TestCase):
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


class CorridorIndexUsageTest(TestCase):
    """assertNumQueries proves count, not query
    plan -- supplement with an EXPLAIN QUERY PLAN assertion that the
    bbox prefilter hits the (latitude, longitude) composite index
    rather than a full table scan."""

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
