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
from decimal import Decimal
from pathlib import Path

from django.test import SimpleTestCase

FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "mapbox_multi_waypoint.json"
)
with open(FIXTURE_PATH, encoding="utf-8") as f:
    MULTI_WAYPOINT_FIXTURE = json.load(f)


def _leg_coordinate_slices(coords, leg_annotation_lengths):
    """Slice `coords` (a route's single combined `geometry.coordinates`
    array) into one coordinate list per Mapbox leg, using each leg's own
    annotation-array length as its segment count. Leg *i* contributes
    `leg_annotation_lengths[i]` segments and therefore
    `leg_annotation_lengths[i] + 1` coordinates; its final coordinate is
    the boundary waypoint shared with the next leg's first coordinate.

    This is a local, test-only mirror of the mechanism that
    `routing.services.multi_leg.leg_coordinate_slices` implements for
    real (see test_leg_coordinate_slices_matches_local_mechanism below,
    added once that module exists)."""
    slices = []
    start = 0
    for length in leg_annotation_lengths:
        end = start + length + 1
        slices.append(coords[start:end])
        start = end - 1
    return slices


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
