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

# Lazily-built (STRtree, list[Station]) pair over Station.objects.routable(),
# in raw lng/lat degree space -- route-independent, built once per process.
# None until first use. Guarded by _INDEX_LOCK (double-checked locking) so
# concurrent first-request builds under a threaded worker model collapse to
# one build; querying an already-built tree is read-only and needs no lock.
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
    equirectangular-scaled planar frame `build_planar_route` uses."""
    stations = list(Station.objects.routable())
    points = [Point(float(s.longitude), float(s.latitude)) for s in stations]
    return STRtree(points), stations


def _get_index():
    """Double-checked-locking lazy accessor. Construction is deliberately
    lazy on first use (D-31) -- never eager in AppConfig.ready(), where
    every management command, migration, and test run would pay the DB
    read, and DB access in ready() runs before migrations are applied."""
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    with _INDEX_LOCK:
        if _INDEX is None:
            _INDEX = _build_index()
        return _INDEX


def reset_index():
    """The sole invalidation hook (D-29): clears the process-level index so
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


def candidates(route) -> list[Candidate]:
    """Return solver-ready Candidate stations within the route's
    precision-tiered perpendicular corridor.

    Queries the lazily-built, process-level STRtree (see `_get_index()`)
    with the raw-degree route geometry buffered by the max corridor width,
    then applies an in-process perpendicular corridor-distance test (never
    the endpoint/chord shortcut) against the equirectangular-scaled route
    polyline. Returns whatever the corridor contains -- no feasibility
    judgement, no re-widening on an empty result.
    """
    rooftop_mi, city_mi = _corridor_widths()

    coords = route.raw_coordinates
    # Computed once per corridor pass, not once per candidate station --
    # see mean_lat_rad()'s docstring. Threaded into every downstream call
    # that would otherwise re-derive it from the same route coordinates.
    mean_lat = mean_lat_rad(coords)
    tree, indexed_stations = _get_index()

    buffer_deg = _corridor_buffer_degrees(coords, city_mi, mean_lat=mean_lat)
    raw_route = LineString(coords)
    query_region = raw_route.buffer(buffer_deg)
    survivor_idx = tree.query(query_region, predicate="intersects")
    stations = [indexed_stations[i] for i in survivor_idx]

    planar_route = build_planar_route(coords, mean_lat=mean_lat)
    route_length_mi = _as_decimal(planar_route.length)
    total_route_mi = route.total_route_mi

    result = []
    for station in stations:
        planar_point = project_point(
            float(station.longitude),
            float(station.latitude),
            coords,
            mean_lat=mean_lat,
        )

        half_width = (
            rooftop_mi
            if station.geocode_precision == GeocodePrecision.ROOFTOP
            else city_mi
        )

        perpendicular_mi = _as_decimal(planar_route.distance(planar_point))
        if perpendicular_mi > half_width:
            continue

        fraction = _as_decimal(planar_route.project(planar_point)) / route_length_mi
        distance_from_start_mi = fraction * total_route_mi
        distance_from_start_mi = max(
            Decimal(0), min(distance_from_start_mi, total_route_mi)
        )

        result.append(
            Candidate(
                name=station.name,
                opis_id=station.opis_id,
                price_per_gallon=station.retail_price,
                distance_from_start_mi=distance_from_start_mi,
            )
        )

    return result
