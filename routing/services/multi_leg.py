"""Flatten a multi-leg Mapbox route into per-leg planar geometry plus a
continuous offset-summed distance-from-start scale (Pitfall 10 & 12).

This module NEVER builds one merged `LineString` spanning multiple legs
and NEVER calls `.project()`/`.distance()` against such a merged line --
a self-overlapping/backtracking multi-stop route can resolve a candidate
to the wrong occurrence on a merged line, silently corrupting stop
ordering. Instead, every Mapbox leg gets its own independent planar
line (built via `routing.services.corridor`'s existing equirectangular
helpers, unchanged), and leg-local positions are combined into ONE
flattened scale by addition (offset-sum) -- never by re-projecting.

Request-path geometry only -- no `routing.pipeline` import (scanned by
`routing/tests/test_boundaries.py::ImportBoundaryTest`'s services/-wide
sweep). Not one of the AST-import-gated solver files (`solver.py`/
`services/exceptions.py`), so it is free to import `corridor.py`'s
Django-settings-backed helpers.
"""
from dataclasses import dataclass
from decimal import Decimal

from routing.services.corridor import (
    SIMPLIFY_TOLERANCE_MI,
    build_planar_route,
    mean_lat_rad,
)


@dataclass(frozen=True)
class FlattenedRoute:
    """A route's per-leg planar geometry plus the offset-sum scale
    needed to place every leg's candidates on one continuous
    distance-from-start axis.

    Every list is index-aligned to Mapbox's own leg order (leg 0 first).
    `leg_boundaries_mi[i]` is the cumulative real-world distance
    (`route.leg_distances_mi`) accumulated over every leg BEFORE leg i --
    e.g. `[0, leg0_mi, leg0_mi + leg1_mi, ...]`.
    """

    leg_coord_slices: list
    leg_mean_lats: list
    leg_lines: list
    leg_planar_lengths_mi: list
    leg_boundaries_mi: list


def leg_coordinate_slices(coords, leg_annotation_lengths):
    """Slice `coords` (a route's single combined `geometry.coordinates`
    array) into one coordinate list per Mapbox leg, using each leg's own
    annotation-array length as its segment count.

    Leg *i* contributes `leg_annotation_lengths[i]` segments and
    therefore `leg_annotation_lengths[i] + 1` coordinates; its final
    coordinate is the boundary waypoint shared with the next leg's
    first coordinate. This is the mechanism settled against a captured
    3-stop fixture (see `routing/tests/test_multi_leg.py`) -- NOT
    snapped-location matching against Mapbox's `waypoints[]` array,
    which offers no reliable geometry-index alignment.
    """
    slices = []
    start = 0
    for length in leg_annotation_lengths:
        end = start + length + 1
        slices.append(coords[start:end])
        start = end - 1
    return slices


def flatten_route(route) -> FlattenedRoute:
    """Build one planar `LineString` PER Mapbox leg (never one merged
    line) plus the offset-sum cumulative distance-from-start scale for
    `route`. Works uniformly for any leg count, including a single leg,
    though `corridor.candidates()` only calls this when there are 2 or
    more legs -- a single-leg route keeps its byte-identical prior
    behavior via the whole-route path."""
    coords = route.raw_coordinates
    slices = leg_coordinate_slices(coords, route.leg_annotation_lengths)

    leg_mean_lats = []
    leg_lines = []
    leg_planar_lengths_mi = []
    for leg_coords in slices:
        mean_lat = mean_lat_rad(leg_coords)
        planar_leg = build_planar_route(leg_coords, mean_lat=mean_lat)
        # Simplified immediately, and the leg's planar length derived from
        # THIS SAME simplified object in this same iteration -- corridor.py's
        # per-leg candidate build later calls .distance()/.line_locate_point()
        # against this exact `planar_leg`, so the two can never drift apart
        # (see this plan's per-leg route_length_mi hazard note).
        planar_leg = planar_leg.simplify(SIMPLIFY_TOLERANCE_MI)
        leg_mean_lats.append(mean_lat)
        leg_lines.append(planar_leg)
        leg_planar_lengths_mi.append(Decimal(str(planar_leg.length)))

    leg_boundaries_mi = [Decimal(0)]
    for leg_mi in route.leg_distances_mi[:-1]:
        leg_boundaries_mi.append(leg_boundaries_mi[-1] + leg_mi)

    return FlattenedRoute(
        leg_coord_slices=slices,
        leg_mean_lats=leg_mean_lats,
        leg_lines=leg_lines,
        leg_planar_lengths_mi=leg_planar_lengths_mi,
        leg_boundaries_mi=leg_boundaries_mi,
    )
