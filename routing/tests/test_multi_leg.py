"""Tests for multi-leg route flattening (Pitfall 10 & 12).

Per-leg coordinate slicing mechanism (settled here, Task 1 of the
multi-stop waypoints phase): leg *i* owns the slice of the route's
single combined `geometry.coordinates` array given by CUMULATIVE
SEGMENT COUNTS -- leg *i* contributes `len(legs[i]["annotation"]["distance"])`
segments and therefore `len(...) + 1` coordinates, with its final
coordinate shared as the boundary waypoint with the next leg's first
coordinate. This is NOT snapped-location matching against Mapbox's
`waypoints[]` array (that array's `location` field cannot be reliably
aligned back to an index in `geometry.coordinates` -- no `geometry_index`
is present on real waypoint entries, only on silent `via_waypoints`,
which do not apply here since every user stop is a real leg-breaking
waypoint). The mechanism is grounded against a captured 3-stop
LA->Denver->Chicago fixture below.
"""
import json
import math
from decimal import Decimal
from pathlib import Path

from django.test import SimpleTestCase, TestCase
from shapely.geometry import LineString

from routing.models import GeocodePrecision, GeocodeStatus, Station
from routing.services.corridor import (
    build_planar_route,
    candidates,
    mean_lat_rad,
    project_point,
    reset_index,
)
from routing.services.mapbox import Route, _parse_single_route
from routing.services.multi_leg import flatten_route, leg_coordinate_slices

FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "mapbox_multi_waypoint.json"
)
with open(FIXTURE_PATH, encoding="utf-8") as f:
    MULTI_WAYPOINT_FIXTURE = json.load(f)

# Task 1's mechanism now lives for real in routing.services.multi_leg;
# tests below use that module directly rather than a local duplicate.
_leg_coordinate_slices = leg_coordinate_slices


class MultiWaypointFixtureShapeTests(SimpleTestCase):
    """The captured fixture has the documented Mapbox multi-leg shape,
    and the settled slicing mechanism reconstructs the full combined
    coordinate array with no gap and no duplication."""

    def test_fixture_is_well_formed(self):
        route = MULTI_WAYPOINT_FIXTURE["routes"][0]
        self.assertEqual(len(route["legs"]), 2)
        self.assertIn("coordinates", route["geometry"])

    def test_sum_of_leg_annotation_lengths_equals_concatenated_axis_length(self):
        route = MULTI_WAYPOINT_FIXTURE["routes"][0]
        legs = route["legs"]

        total_annotation_length = sum(
            len(leg["annotation"]["distance"]) for leg in legs
        )
        concatenated = [
            v for leg in legs for v in leg["annotation"]["distance"]
        ]

        self.assertEqual(total_annotation_length, len(concatenated))
        # The concatenated annotation axis always has exactly one fewer
        # entry than the combined coordinate array (segments, not points).
        self.assertEqual(
            total_annotation_length,
            len(route["geometry"]["coordinates"]) - 1,
        )

    def test_leg_coordinate_slices_reconstruct_full_combined_array(self):
        route = MULTI_WAYPOINT_FIXTURE["routes"][0]
        coords = route["geometry"]["coordinates"]
        legs = route["legs"]
        leg_annotation_lengths = [len(leg["annotation"]["distance"]) for leg in legs]

        slices = _leg_coordinate_slices(coords, leg_annotation_lengths)

        self.assertEqual(len(slices), 2)
        # Each leg contributes `length + 1` coordinates.
        for leg_slice, length in zip(slices, leg_annotation_lengths):
            self.assertEqual(len(leg_slice), length + 1)

        # No gap, no duplication when flattening the per-leg slices back
        # into one walk: the first slice in full, then every later
        # slice minus its shared leading (boundary) coordinate.
        reconstructed = list(slices[0])
        for leg_slice in slices[1:]:
            reconstructed.extend(leg_slice[1:])
        self.assertEqual(reconstructed, coords)

        # The interior boundary coordinate is shared, not duplicated:
        # leg 0's last coordinate equals leg 1's first coordinate.
        self.assertEqual(slices[0][-1], slices[1][0])

    def test_boundary_coordinate_matches_denver_waypoint(self):
        route = MULTI_WAYPOINT_FIXTURE["routes"][0]
        coords = route["geometry"]["coordinates"]
        legs = route["legs"]
        leg_annotation_lengths = [len(leg["annotation"]["distance"]) for leg in legs]

        slices = _leg_coordinate_slices(coords, leg_annotation_lengths)
        denver_waypoint = MULTI_WAYPOINT_FIXTURE["waypoints"][1]["location"]

        self.assertEqual(slices[0][-1], denver_waypoint)


class FlattenRouteTests(SimpleTestCase):
    """`flatten_route()` builds one planar `LineString` PER Mapbox leg
    (never one merged line spanning legs) and an offset-sum boundary
    scale derived from the route's own per-leg distances."""

    def _la_denver_chicago_route(self) -> Route:
        return _parse_single_route(MULTI_WAYPOINT_FIXTURE["routes"][0], 0)

    def test_builds_exactly_one_line_per_leg(self):
        route = self._la_denver_chicago_route()
        flattened = flatten_route(route)

        self.assertEqual(len(flattened.leg_lines), 2)
        self.assertEqual(len(flattened.leg_coord_slices), 2)
        self.assertEqual(len(flattened.leg_mean_lats), 2)
        self.assertEqual(len(flattened.leg_planar_lengths_mi), 2)

    def test_each_leg_line_is_built_only_from_its_own_slice(self):
        """Regression guard for the anti-pattern this module exists to
        avoid: no leg's planar line spans more coordinates than that
        leg's own slice -- never the full combined route."""
        route = self._la_denver_chicago_route()
        flattened = flatten_route(route)

        for leg_line, leg_coords in zip(flattened.leg_lines, flattened.leg_coord_slices):
            self.assertEqual(len(leg_line.coords), len(leg_coords))
            # Never as many points as the whole combined route (unless a
            # leg degenerately IS the whole route, not the case here).
            self.assertLess(len(leg_line.coords), len(route.raw_coordinates))

    def test_leg_boundaries_are_cumulative_real_leg_distances(self):
        route = self._la_denver_chicago_route()
        flattened = flatten_route(route)

        self.assertEqual(flattened.leg_boundaries_mi[0], Decimal(0))
        self.assertEqual(flattened.leg_boundaries_mi[1], route.leg_distances_mi[0])
        self.assertEqual(len(flattened.leg_boundaries_mi), len(route.leg_distances_mi))


def _make_station(opis_id, latitude, longitude, geocode_precision=GeocodePrecision.ROOFTOP):
    return Station.objects.create(
        opis_id=opis_id,
        name=f"Station {opis_id}",
        address="I-00, EXIT 1 & US-1",
        city="Anytown",
        state="OK",
        rack_id="100",
        retail_price=Decimal("3.259"),
        geocode_status=GeocodeStatus.OK,
        geocode_precision=geocode_precision,
        latitude=latitude,
        longitude=longitude,
        observation_count=1,
        price_min=Decimal("3.259"),
        price_max=Decimal("3.259"),
    )


class DetourCorridorTestCase(TestCase):
    """A deliberately detour-shaped, self-crossing 3-leg route:
    A -> B -> C -> D, where leg CD crosses back directly over leg AB's
    path (Pitfall 12's "significant detour waypoint" warning sign).
    Coordinates and expected per-leg positions are pre-computed and
    documented inline -- see 13-01-SUMMARY.md for the derivation."""

    A = (Decimal("-100.0"), Decimal("40.0"))
    B = (Decimal("-90.0"), Decimal("40.0"))
    C = (Decimal("-90.0"), Decimal("45.0"))
    D = (Decimal("-100.0"), Decimal("39.5"))

    LEG_DISTANCES_MI = [
        Decimal("529.8882621942594"),
        Decimal("345.8599999999997"),
        Decimal("637.892946361353"),
    ]

    def setUp(self):
        super().setUp()
        reset_index()

    def _route(self):
        coords = [
            [float(self.A[0]), float(self.A[1])],
            [float(self.B[0]), float(self.B[1])],
            [float(self.C[0]), float(self.C[1])],
            [float(self.D[0]), float(self.D[1])],
        ]
        return Route(
            total_route_mi=sum(self.LEG_DISTANCES_MI, Decimal(0)),
            geometry=LineString(coords),
            raw_coordinates=coords,
            leg_distances_mi=self.LEG_DISTANCES_MI,
            leg_annotation_lengths=[1, 1, 1],
        )


class DetourOrderingTests(DetourCorridorTestCase):
    """Stations placed unambiguously on each leg of the detour-shaped
    route come back from `candidates()` in strictly increasing
    `distance_from_start_mi` order -- true travel order (A->B->C->D),
    never corrupted by the route's own self-crossing shape."""

    def test_stations_on_each_leg_are_ordered_by_true_travel_order(self):
        early = _make_station(301, latitude=Decimal("40.0"), longitude=Decimal("-93.0"))
        mid = _make_station(302, latitude=Decimal("42.0"), longitude=Decimal("-90.0"))
        late = _make_station(303, latitude=Decimal("41.15"), longitude=Decimal("-97.0"))

        result = candidates(self._route())
        by_opis_id = {c.opis_id: c for c in result}

        self.assertEqual(
            {c.opis_id for c in result}, {early.opis_id, mid.opis_id, late.opis_id}
        )
        self.assertLess(
            by_opis_id[early.opis_id].distance_from_start_mi,
            by_opis_id[mid.opis_id].distance_from_start_mi,
        )
        self.assertLess(
            by_opis_id[mid.opis_id].distance_from_start_mi,
            by_opis_id[late.opis_id].distance_from_start_mi,
        )

        # Each station lands inside its own leg's flattened range.
        self.assertAlmostEqual(
            float(by_opis_id[early.opis_id].distance_from_start_mi), 370.92, delta=2.0
        )
        self.assertAlmostEqual(
            float(by_opis_id[mid.opis_id].distance_from_start_mi), 668.23, delta=2.0
        )
        self.assertAlmostEqual(
            float(by_opis_id[late.opis_id].distance_from_start_mi), 1322.27, delta=2.0
        )


class SingleLegRegressionTests(TestCase):
    """A single-leg route (`leg_distances_mi` has exactly one entry, or
    is empty on a `Route` predating these fields) takes the exact same
    single-corridor path as before -- byte-identical
    `distance_from_start_mi` output."""

    def setUp(self):
        super().setUp()
        reset_index()

    ROUTE_COORDS = [[-97.00, 30.00], [-97.00, 40.00]]
    TOTAL_ROUTE_MI = Decimal("700")

    def test_populated_single_element_leg_fields_match_default_empty_fields(self):
        _make_station(401, latitude=Decimal("32.50"), longitude=Decimal("-97.00"))

        route_without_leg_fields = Route(
            total_route_mi=self.TOTAL_ROUTE_MI,
            geometry=LineString(self.ROUTE_COORDS),
            raw_coordinates=self.ROUTE_COORDS,
        )
        route_with_one_leg = Route(
            total_route_mi=self.TOTAL_ROUTE_MI,
            geometry=LineString(self.ROUTE_COORDS),
            raw_coordinates=self.ROUTE_COORDS,
            leg_distances_mi=[self.TOTAL_ROUTE_MI],
            leg_annotation_lengths=[1],
        )

        result_without = candidates(route_without_leg_fields)
        result_with = candidates(route_with_one_leg)

        self.assertEqual(len(result_without), 1)
        self.assertEqual(
            result_without[0].distance_from_start_mi,
            result_with[0].distance_from_start_mi,
        )


class MultiLegSimplifyLengthInvariantTests(TestCase):
    """Multi-leg analogue of `CorridorSimplifyLengthInvariantTests`
    (`routing/tests/test_corridor.py`): each leg's `leg_planar_lengths_mi`
    entry must be derived from the SAME simplified per-leg planar line
    the vectorized `.distance()`/`.line_locate_point()` calls in
    `_candidates_multi_leg` run against -- never from a stale
    full-resolution leg length. Every pre-existing multi-leg test above
    uses 1-segment legs, where `.simplify()` is a structural no-op and
    cannot exercise this hazard class at all.

    A PERFECTLY STRAIGHT densified leg (all points collinear) is NOT a
    sufficient fixture here, even though `.simplify()` measurably drops
    its vertex count: for a straight line, the sum of consecutive
    segment lengths always equals the straight-line distance exactly
    (no chord ever "cuts a corner"), so the pre- and post-simplification
    lengths are bit-for-bit identical and the hazard this test exists to
    catch cannot manifest. This fixture instead traces a gentle
    high-frequency sine wiggle (amplitude comfortably under
    `SIMPLIFY_TOLERANCE_MI`'s 0.05 mi threshold, so `.simplify()` still
    irons leg 1 down to its 2 bounding vertices) around each leg's
    north-south trend line -- real curvature that `.simplify()`
    genuinely shortens, so a stale pre-simplification length measurably
    disagrees with the correct one."""

    LEG0_REAL_MI = Decimal("400")
    LEG1_REAL_MI = Decimal("400")
    N_POINTS_PER_LEG = 6000
    WIGGLE_AMPLITUDE_DEG = 0.0009
    WIGGLE_CYCLES = 400

    def setUp(self):
        super().setUp()
        reset_index()

    def _leg_coords(self, lat_start, lat_end):
        n = self.N_POINTS_PER_LEG
        coords = []
        for i in range(n):
            t = i / (n - 1)
            lat = lat_start + (lat_end - lat_start) * t
            lng = -97.00 + self.WIGGLE_AMPLITUDE_DEG * math.sin(
                2 * math.pi * self.WIGGLE_CYCLES * t
            )
            coords.append([lng, lat])
        return coords

    def _route(self):
        # Two wiggly north-south legs sharing a boundary waypoint at
        # lat 35.00 -- the shared coordinate is included once, per
        # `leg_coordinate_slices`'s documented mechanism.
        leg0_coords = self._leg_coords(30.00, 35.00)
        leg1_coords = self._leg_coords(35.00, 40.00)
        combined = leg0_coords + leg1_coords[1:]
        return Route(
            total_route_mi=self.LEG0_REAL_MI + self.LEG1_REAL_MI,
            geometry=LineString(combined),
            raw_coordinates=combined,
            leg_distances_mi=[self.LEG0_REAL_MI, self.LEG1_REAL_MI],
            leg_annotation_lengths=[
                self.N_POINTS_PER_LEG - 1,
                self.N_POINTS_PER_LEG - 1,
            ],
        )

    def test_simplification_reduces_vertex_count_on_both_legs(self):
        """Sanity check that this fixture actually exercises
        simplification on EVERY leg -- otherwise the invariant test
        below would pass vacuously."""
        flattened = flatten_route(self._route())

        self.assertEqual(len(flattened.leg_lines), 2)
        for leg_line, leg_coords in zip(
            flattened.leg_lines, flattened.leg_coord_slices
        ):
            self.assertLess(len(leg_line.coords), len(leg_coords))

    def test_known_fraction_station_positions_correctly_on_simplified_leg(self):
        # 36.25 is exactly 25% of the way from 35.00 to 40.00, on leg 1
        # (the SECOND leg) -- this also exercises the offset-sum
        # boundary addition (leg 0's full 400 mi plus leg 1's own
        # fraction), not just an isolated in-leg fraction. Leg 1's
        # simplified line collapses fully to its 2 bounding vertices, so
        # the correct answer lands on the analytic 25% mark essentially
        # exactly -- a stale-length regression shifts it by several
        # miles (verified via temporary sabotage during this task; see
        # the corresponding SUMMARY/commit).
        _make_station(
            501,
            latitude=Decimal("36.25"),
            longitude=Decimal("-97.00"),
        )

        result = candidates(self._route())

        self.assertEqual(len(result), 1)
        expected = self.LEG0_REAL_MI + self.LEG1_REAL_MI * Decimal("0.25")
        self.assertAlmostEqual(
            float(result[0].distance_from_start_mi), float(expected), delta=1.0
        )


class MergedLineMisorderingRegressionTests(SimpleTestCase):
    """Pure-geometry demonstration of exactly why `multi_leg.py` never
    builds or projects onto a merged multi-leg line (Pitfall 12).

    At the detour route's own self-crossing point, leg AB and leg CD
    physically cross -- the same coordinate is genuinely reachable at
    TWO different points in true travel order (early, on the outbound
    leg; and again near the very end, on the return leg). Projecting
    that point onto ONE merged `LineString` spanning the whole route
    silently collapses it to the EARLIER occurrence, discarding the
    late one entirely -- the exact misordering this codebase avoids by
    never building that merged line in the first place."""

    A = (-100.0, 40.0)
    B = (-90.0, 40.0)
    C = (-90.0, 45.0)
    D = (-100.0, 39.5)
    # The true intersection of segments AB and CD (verified algebraically
    # and empirically against shapely: both segments' finite ranges
    # contain this point).
    CROSSING_POINT = (-99.0909090909091, 40.0)

    def test_merged_line_projection_collapses_to_the_early_occurrence(self):
        coords = [self.A, self.B, self.C, self.D]
        mean_lat = mean_lat_rad(coords)
        merged = build_planar_route(coords, mean_lat=mean_lat)
        point = project_point(*self.CROSSING_POINT, coords, mean_lat=mean_lat)

        # Merged-line distance to the crossing point is ~0 (it is
        # genuinely ON the merged line), yet .project() reports a
        # position well inside leg AB's own ~530 mi span -- not the
        # ~1456 mi position where this same coordinate is also, and
        # chronologically more meaningfully, reached again near FINISH.
        self.assertLess(merged.distance(point), 1.0)
        self.assertLess(merged.project(point), 530.0)

    def test_per_leg_projection_preserves_the_late_occurrence(self):
        """The same crossing coordinate, evaluated against ONLY leg
        CD's own planar line (as `multi_leg.flatten_route()` does),
        correctly lands near the end of the flattened route -- proving
        the late occurrence is never lost when legs are never merged."""
        route = Route(
            total_route_mi=Decimal("1513.6412085556121"),
            geometry=LineString([self.A, self.B, self.C, self.D]),
            raw_coordinates=[list(self.A), list(self.B), list(self.C), list(self.D)],
            leg_distances_mi=[
                Decimal("529.8882621942594"),
                Decimal("345.8599999999997"),
                Decimal("637.892946361353"),
            ],
            leg_annotation_lengths=[1, 1, 1],
        )
        flattened = flatten_route(route)
        leg_index = 2  # CD
        mean_lat = flattened.leg_mean_lats[leg_index]
        point = project_point(
            *self.CROSSING_POINT, flattened.leg_coord_slices[leg_index], mean_lat=mean_lat
        )
        planar_leg = flattened.leg_lines[leg_index]
        leg_planar_length_mi = flattened.leg_planar_lengths_mi[leg_index]

        fraction = Decimal(str(planar_leg.project(point))) / leg_planar_length_mi
        distance_from_start_mi = flattened.leg_boundaries_mi[
            leg_index
        ] + fraction * route.leg_distances_mi[leg_index]

        self.assertGreater(distance_from_start_mi, Decimal("1400"))
