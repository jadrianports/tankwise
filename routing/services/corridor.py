"""Corridor filter: candidate fuel stops in the route's precision-tiered
perpendicular corridor.

Request-path geometry over the routable station set -- never imports
the offline geocoding pipeline package. Money/measure values
stay exact, unrounded `Decimal`; shapely's float outputs are coerced via
`Decimal(str(value))`, never `Decimal(float)`, mirroring solver.py's
`_as_decimal` discipline.

The corridor prefilter runs through a lazily-built, process-level STRtree
(`_INDEX`) rather than a per-request DB query -- see `_get_index()` /
`reset_index()`. `reset_index()` is the sole invalidation hook: it is
called from `seed_stations` (the only runtime write path) and from a
test `setUp`, never from `candidates()` itself.
"""
import math
import threading
from decimal import Decimal
from typing import NamedTuple

import shapely
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from shapely import STRtree
from shapely.geometry import LineString, Point

from routing.models import GeocodePrecision, Station
from routing.services.solver import Candidate

# ~111.32 km/deg (WGS-84 latitude) / 1.609344 km/mi. Longitude is
# additionally scaled by cos(mean_lat) below (equirectangular
# projection).
MI_PER_DEGREE_LAT = Decimal("69.172")

# `.simplify()` tolerance for the planar (mile-scaled) route/leg line used
# for the precise perpendicular-distance/along-line test, in MILES -- the
# planar frame is already scaled to real miles (MI_PER_DEGREE_LAT x
# cos(mean_lat)), so this needs no unit conversion. Pinned on a measured
# sweep (see this plan's evidence): candidate sets held identical through
# 0.10 mi and first diverged at 0.25 mi on 5,000-vertex synthetic routes.
# 0.05 mi sits two tiers of margin below that first divergence, and ~100x
# below the 5-mi rooftop corridor half-width -- geometrically negligible
# next to either the corridor width or the divergence point.
SIMPLIFY_TOLERANCE_MI = 0.05


class IndexedStation(NamedTuple):
    """The seven `Station` attributes the corridor path actually reads --
    `_candidates_single_leg`/`_candidates_multi_leg` access `.name`,
    `.retail_price`, `.geocode_precision`, etc. exactly as they would on a
    full `Station` instance, so swapping the index's row type here needs
    no change at either call site. Deliberately narrower than `Station`'s
    15 concrete columns -- `_build_index()` fetches only these seven, since
    that fetch crosses the network to Neon on every per-worker cold start."""

    opis_id: int
    name: str
    state: str
    retail_price: Decimal
    latitude: Decimal
    longitude: Decimal
    geocode_precision: str | None


# Lazily-built (STRtree, list[IndexedStation]) pair over
# Station.objects.routable(), in raw lng/lat degree space -- route-independent,
# built once per process. None until first use. Guarded by _INDEX_LOCK
# (double-checked locking) so concurrent first-request builds under a
# threaded worker model collapse to one build; querying an already-built
# tree is read-only and needs no lock.
_INDEX = None
_INDEX_LOCK = threading.Lock()


def _as_decimal(value):
    return value if isinstance(value, Decimal) else Decimal(str(value))


def mean_lat_rad(coords_lnglat):
    """Mean latitude of the route, in radians -- the single shared scaling
    reference for build_planar_route() and project_point(), so the projection
    distortion stays consistent route-wide.

    Callers on the hot path (e.g. candidates()'s per-station loop) should
    compute this ONCE and pass it down via each function's `mean_lat=`
    keyword rather than letting every call re-sum the whole route -- the
    shared value is what keeps build_planar_route() and project_point() in
    the same projection frame, and re-deriving it per station is an
    avoidable O(route_points) cost repeated once per candidate."""
    lats = [lat for _lng, lat in coords_lnglat]
    return math.radians(sum(lats) / len(lats))


def build_planar_route(coords_lnglat, *, mean_lat=None):
    """Build a shapely LineString scaled to real miles via an equirectangular
    projection (cos(mean_lat) x MI_PER_DEGREE_LAT), so .distance()/.project()
    operate on real miles, never raw degrees. `coords_lnglat`:
    Mapbox's raw `[lng, lat]` GeoJSON pairs (route.raw_coordinates).

    `mean_lat`: pre-computed mean_lat_rad(coords_lnglat), in radians. When
    omitted, falls back to computing it here (backward compatible)."""
    if mean_lat is None:
        mean_lat = mean_lat_rad(coords_lnglat)
    cos_lat = math.cos(mean_lat)
    scale = float(MI_PER_DEGREE_LAT)
    points = [(lng * scale * cos_lat, lat * scale) for lng, lat in coords_lnglat]
    return LineString(points)


def project_point(lng, lat, coords_lnglat, *, mean_lat=None):
    """Project a single station (lng, lat) into the SAME planar-mile frame
    as build_planar_route(coords_lnglat) -- same mean-lat reference -- so
    the perpendicular-distance comparison is apples to apples.

    `mean_lat`: pre-computed mean_lat_rad(coords_lnglat), in radians. When
    omitted, falls back to computing it here (backward compatible)."""
    if mean_lat is None:
        mean_lat = mean_lat_rad(coords_lnglat)
    cos_lat = math.cos(mean_lat)
    scale = float(MI_PER_DEGREE_LAT)
    return Point(lng * scale * cos_lat, lat * scale)


def _corridor_widths():
    rooftop_mi = _as_decimal(settings.CORRIDOR_ROOFTOP_MI)
    city_mi = _as_decimal(settings.CORRIDOR_CITY_MI)
    if rooftop_mi <= 0:
        raise ImproperlyConfigured(
            f"CORRIDOR_ROOFTOP_MI must be positive, got {rooftop_mi}"
        )
    if city_mi <= 0:
        raise ImproperlyConfigured(
            f"CORRIDOR_CITY_MI must be positive, got {city_mi}"
        )
    return rooftop_mi, city_mi


def _route_bbox(coords_lnglat):
    """Kept for `benchmark_corridor`'s historical-baseline timing only --
    the request path no longer calls this (see `candidates()`)."""
    lats = [lat for _lng, lat in coords_lnglat]
    lngs = [lng for lng, _lat in coords_lnglat]
    return (
        _as_decimal(min(lats)),
        _as_decimal(max(lats)),
        _as_decimal(min(lngs)),
        _as_decimal(max(lngs)),
    )


def _build_index():
    """Materialize the routable station set and a parallel STRtree of raw
    lng/lat degree Points. Route-independent -- built once per process, in
    the tree's native (unscaled) coordinate space, never the
    equirectangular-scaled planar frame `build_planar_route` uses.

    Fetches exactly the seven columns the corridor path reads (via
    `values_list`, never full `Station` instances) -- narrowing 15 columns
    to 7 matters far more over the network to Neon than it does locally,
    and every per-worker cold start pays this fetch again. Point
    construction is a single vectorized `shapely.points()` call rather than
    a per-row `Point(...)` list comprehension."""
    rows = Station.objects.routable().values_list(
        "opis_id", "name", "state", "retail_price", "latitude", "longitude",
        "geocode_precision",
    )
    stations = [IndexedStation(*row) for row in rows]
    xs = [float(s.longitude) for s in stations]
    ys = [float(s.latitude) for s in stations]
    points = shapely.points(xs, ys)
    return STRtree(points), stations


def _get_index():
    """Double-checked-locking lazy accessor. Construction is deliberately
    lazy on first use -- never eager in AppConfig.ready(), where
    every management command, migration, and test run would pay the DB
    read, and DB access in ready() runs before migrations are applied."""
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    with _INDEX_LOCK:
        if _INDEX is None:
            _INDEX = _build_index()
        return _INDEX


def warm_index():
    """Explicitly-callable handle on `_get_index()`'s lazy build, so a
    caller (views.py's `_solve_all_alternatives`) can force the one-time
    index build to happen inside its own timing stage, BEFORE the
    per-alternative corridor stages -- otherwise the first `candidates()`
    call of a process silently bills the index build's cost against
    whichever alternative happens to run first, conflating a one-time
    process-level cost with per-route geometry work. Returns nothing;
    does not change `_get_index()`, `_build_index()`, `reset_index()`, or
    the double-checked locking behaviour."""
    _get_index()


def reset_index():
    """The sole invalidation hook: clears the process-level index so
    the next `candidates()` call rebuilds it from the current Station
    table. Called from `seed_stations` after a reseed commits, and from an
    autouse test fixture -- never from `candidates()` itself, which would
    defeat the entire point of removing the per-request DB query."""
    global _INDEX
    _INDEX = None


def _corridor_buffer_degrees(coords_lnglat, city_mi, *, mean_lat=None):
    """Single isotropic buffer distance, in degrees, for the raw-degree
    STRtree query. shapely's `.buffer()` takes one scalar and cannot apply
    a per-axis pad the way the old bbox query's separate lat_pad/lng_pad
    could -- so this returns the LARGER of the two axis pads. Because
    cos(lat) <= 1, that is always lng_pad, which makes the buffer provably
    over-inclusive (never under-inclusive) along the north-south axis; the
    unchanged precise perpendicular test below discards the extras.

    `mean_lat`: pre-computed mean_lat_rad(coords_lnglat), in radians. When
    omitted, falls back to computing it here (backward compatible)."""
    if mean_lat is None:
        mean_lat = mean_lat_rad(coords_lnglat)
    cos_lat = math.cos(mean_lat)
    lat_pad = city_mi / MI_PER_DEGREE_LAT
    lng_pad = city_mi / (MI_PER_DEGREE_LAT * _as_decimal(max(abs(cos_lat), 0.01)))
    return float(max(lat_pad, lng_pad))


def _planar_points_for_stations(stations, mean_lat):
    """Vectorized equivalent of calling `project_point()` once per station:
    the identical equirectangular scaling (same `mean_lat`, same
    `MI_PER_DEGREE_LAT`), built as one `shapely.points()` call over two
    float lists instead of a per-station `Point(...)` construction --
    verified bit-identical output to the scalar per-station calls it
    replaces. Shared by both the single-leg and multi-leg candidate
    builds so the two paths can never drift apart on this formula."""
    cos_lat = math.cos(mean_lat)
    scale = float(MI_PER_DEGREE_LAT)
    xs = [float(s.longitude) * scale * cos_lat for s in stations]
    ys = [float(s.latitude) * scale for s in stations]
    return shapely.points(xs, ys)


def _no_op_factor(_state):
    """Default `factor_for` when the caller passes none -- neutral 1.0
    for every state, preserving pre-EIA-indexing behavior exactly."""
    return Decimal("1")


def candidates(route, factor_for=None) -> list[Candidate]:
    """Return solver-ready Candidate stations within the route's
    precision-tiered perpendicular corridor.

    Queries the lazily-built, process-level STRtree (see `_get_index()`)
    with the raw-degree route geometry buffered by the max corridor width,
    then applies an in-process perpendicular corridor-distance test (never
    the endpoint/chord shortcut) against the equirectangular-scaled route
    polyline. Returns whatever the corridor contains -- no feasibility
    judgement, no re-widening on an empty result.

    `factor_for`: an optional `state -> Decimal` callable (see
    `routing.services.eia.make_factor_lookup`) applied to each station's
    price at Candidate construction -- the single seam where the EIA
    regional index multiplies `station.retail_price`. Defaults to a
    neutral 1.0 no-op so every existing caller that only passes `route`
    keeps its exact prior behavior.

    When `route.leg_distances_mi` carries more than one entry (a
    multi-stop route), delegates to the multi-leg-aware build (Pitfall
    10 & 12): stations are queried and projected per-leg, then placed on
    one flattened distance-from-start scale via offset-sum -- never by
    projecting onto a single merged multi-leg line. A single-leg route
    (today's plain A->B trips, or a `Route` predating these fields)
    keeps this exact byte-identical single-corridor path.
    """
    if factor_for is None:
        factor_for = _no_op_factor

    if len(route.leg_distances_mi) > 1:
        return _candidates_multi_leg(route, factor_for)

    return _candidates_single_leg(route, factor_for)


def _candidates_single_leg(route, factor_for) -> list[Candidate]:
    """The original whole-route single-corridor build -- same STRtree
    prefilter and precision-tiered threshold test as before, but the
    precise perpendicular-distance/along-line pass now runs against a
    SIMPLIFIED planar route with vectorized shapely calls, not a
    per-station Python loop."""
    rooftop_mi, city_mi = _corridor_widths()

    coords = route.raw_coordinates
    # Computed once per corridor pass, not once per candidate station --
    # see mean_lat_rad()'s docstring. Threaded into every downstream call
    # that would otherwise re-derive it from the same route coordinates.
    mean_lat = mean_lat_rad(coords)
    tree, indexed_stations = _get_index()

    # The STRtree prefilter query always runs against the RAW (unsimplified)
    # degree-space route -- simplification only ever applies to the planar
    # line used for the precise perpendicular test below, never to this
    # coarse over-inclusive prefilter.
    buffer_deg = _corridor_buffer_degrees(coords, city_mi, mean_lat=mean_lat)
    raw_route = LineString(coords)
    query_region = raw_route.buffer(buffer_deg)
    survivor_idx = tree.query(query_region, predicate="intersects")
    stations = [indexed_stations[i] for i in survivor_idx]

    # `route_length_mi` MUST come from the SAME (simplified) object passed
    # to the vectorized `.distance()`/`.project()` calls below -- computed
    # immediately after simplifying, in this same function, so the two can
    # never drift apart (see this plan's route_length_mi hazard note).
    planar_route = build_planar_route(coords, mean_lat=mean_lat)
    planar_route = planar_route.simplify(SIMPLIFY_TOLERANCE_MI)
    route_length_mi = _as_decimal(planar_route.length)
    total_route_mi = route.total_route_mi

    planar_points = _planar_points_for_stations(stations, mean_lat)
    perpendicular_mi_raw = shapely.distance(planar_route, planar_points)
    along_line_mi_raw = shapely.line_locate_point(planar_route, planar_points)

    result = []
    for station, perp_raw, along_raw in zip(
        stations, perpendicular_mi_raw, along_line_mi_raw
    ):
        half_width = (
            rooftop_mi
            if station.geocode_precision == GeocodePrecision.ROOFTOP
            else city_mi
        )

        # float() first -- verified to produce identical Decimals to the
        # numpy scalar directly, but kept as defensive clarity, not a
        # behaviour change.
        perpendicular_mi = _as_decimal(float(perp_raw))
        if perpendicular_mi > half_width:
            continue

        fraction = _as_decimal(float(along_raw)) / route_length_mi
        distance_from_start_mi = fraction * total_route_mi
        distance_from_start_mi = max(
            Decimal(0), min(distance_from_start_mi, total_route_mi)
        )

        result.append(
            Candidate(
                name=station.name,
                opis_id=station.opis_id,
                # The phase's single factor-application point (EIA-01/D-02):
                # station.retail_price itself is never reassigned -- only
                # this derived Candidate price is scaled. A factor that
                # reorders which stations are cheapest is the intended
                # outcome, not a bug (see corridor/solver tests).
                price_per_gallon=station.retail_price * factor_for(station.state),
                distance_from_start_mi=distance_from_start_mi,
            )
        )

    return result


def _candidates_multi_leg(route, factor_for) -> list[Candidate]:
    """Multi-leg-aware candidate build (Pitfall 10 & 12): queries
    stations per Mapbox leg (STRtree scoped to that leg's own
    bbox+buffer), projects each candidate against its OWN leg's planar
    line, and places it on the single flattened distance-from-start
    scale via offset-sum -- never by projecting onto one merged
    multi-leg line (a self-overlapping/backtracking route can resolve a
    candidate to the wrong occurrence on a merged line).

    A station within corridor width of two adjacent legs (typically one
    that sits close to the shared boundary waypoint) is deduplicated to
    the single occurrence with the smaller perpendicular distance --
    the local import of `multi_leg` here (rather than at module level)
    breaks the corridor.py <-> multi_leg.py import cycle (multi_leg.py
    imports this module's `build_planar_route`/`mean_lat_rad` helpers).
    """
    from routing.services.multi_leg import flatten_route

    rooftop_mi, city_mi = _corridor_widths()
    flattened = flatten_route(route)
    tree, indexed_stations = _get_index()

    best_by_opis_id = {}
    for leg_index, leg_coords in enumerate(flattened.leg_coord_slices):
        mean_lat = flattened.leg_mean_lats[leg_index]
        planar_leg = flattened.leg_lines[leg_index]
        leg_planar_length_mi = flattened.leg_planar_lengths_mi[leg_index]
        leg_real_mi = route.leg_distances_mi[leg_index]
        offset_mi = flattened.leg_boundaries_mi[leg_index]

        if leg_planar_length_mi == 0:
            continue

        buffer_deg = _corridor_buffer_degrees(leg_coords, city_mi, mean_lat=mean_lat)
        raw_leg = LineString(leg_coords)
        query_region = raw_leg.buffer(buffer_deg)
        survivor_idx = tree.query(query_region, predicate="intersects")
        stations = [indexed_stations[i] for i in survivor_idx]

        # `planar_leg` and `leg_planar_length_mi` are already derived
        # together, from the SAME simplified leg line, inside
        # `flatten_route()` -- see this plan's per-leg route_length_mi
        # hazard note. The vectorized distance/along-line pass below runs
        # against that same simplified `planar_leg`.
        planar_points = _planar_points_for_stations(stations, mean_lat)
        perpendicular_mi_raw = shapely.distance(planar_leg, planar_points)
        along_line_mi_raw = shapely.line_locate_point(planar_leg, planar_points)

        for station, perp_raw, along_raw in zip(
            stations, perpendicular_mi_raw, along_line_mi_raw
        ):
            half_width = (
                rooftop_mi
                if station.geocode_precision == GeocodePrecision.ROOFTOP
                else city_mi
            )

            perpendicular_mi = _as_decimal(float(perp_raw))
            if perpendicular_mi > half_width:
                continue

            fraction = _as_decimal(float(along_raw)) / leg_planar_length_mi
            local_mi = fraction * leg_real_mi
            local_mi = max(Decimal(0), min(local_mi, leg_real_mi))
            distance_from_start_mi = offset_mi + local_mi

            candidate = Candidate(
                name=station.name,
                opis_id=station.opis_id,
                price_per_gallon=station.retail_price * factor_for(station.state),
                distance_from_start_mi=distance_from_start_mi,
            )

            existing = best_by_opis_id.get(station.opis_id)
            if existing is None or perpendicular_mi < existing[0]:
                best_by_opis_id[station.opis_id] = (perpendicular_mi, candidate)

    return [candidate for _, candidate in best_by_opis_id.values()]
