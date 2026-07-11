"""Corridor filter: candidate fuel stops in the route's precision-tiered
perpendicular corridor.

Request-path geometry over the routable station set -- never imports
the offline geocoding pipeline package. Money/measure values
stay exact, unrounded `Decimal`; shapely's float outputs are coerced via
`Decimal(str(value))`, never `Decimal(float)`, mirroring solver.py's
`_as_decimal` discipline.
"""
import math
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from shapely.geometry import LineString, Point

from routing.models import GeocodePrecision, Station
from routing.services.solver import Candidate

# ~111.32 km/deg (WGS-84 latitude) / 1.609344 km/mi. Longitude is
# additionally scaled by cos(mean_lat) below (equirectangular
# projection).
MI_PER_DEGREE_LAT = Decimal("69.172")


def _as_decimal(value):
    return value if isinstance(value, Decimal) else Decimal(str(value))


def mean_lat_rad(coords_lnglat):
    """Mean latitude of the route, in radians -- the single shared scaling
    reference for build_planar_route() and project_point(), so the projection
    distortion stays consistent route-wide."""
    lats = [lat for _lng, lat in coords_lnglat]
    return math.radians(sum(lats) / len(lats))


def build_planar_route(coords_lnglat):
    """Build a shapely LineString scaled to real miles via an equirectangular
    projection (cos(mean_lat) x MI_PER_DEGREE_LAT), so .distance()/.project()
    operate on real miles, never raw degrees. `coords_lnglat`:
    Mapbox's raw `[lng, lat]` GeoJSON pairs (route.raw_coordinates)."""
    cos_lat = math.cos(mean_lat_rad(coords_lnglat))
    scale = float(MI_PER_DEGREE_LAT)
    points = [(lng * scale * cos_lat, lat * scale) for lng, lat in coords_lnglat]
    return LineString(points)


def project_point(lng, lat, coords_lnglat):
    """Project a single station (lng, lat) into the SAME planar-mile frame
    as build_planar_route(coords_lnglat) -- same mean-lat reference -- so
    the perpendicular-distance comparison is apples to apples."""
    cos_lat = math.cos(mean_lat_rad(coords_lnglat))
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
    lats = [lat for _lng, lat in coords_lnglat]
    lngs = [lng for lng, _lat in coords_lnglat]
    return (
        _as_decimal(min(lats)),
        _as_decimal(max(lats)),
        _as_decimal(min(lngs)),
        _as_decimal(max(lngs)),
    )


def candidates(route) -> list[Candidate]:
    """Return solver-ready Candidate stations within the route's
    precision-tiered perpendicular corridor.

    Runs exactly one index-backed bbox query over
    Station.objects.routable(), then applies an in-process perpendicular
    corridor-distance test (never the endpoint/chord shortcut)
    against the equirectangular-scaled route polyline. Returns whatever
    the corridor contains -- no feasibility judgement, no re-widening on
    an empty result.
    """
    rooftop_mi, city_mi = _corridor_widths()

    coords = route.raw_coordinates
    min_lat, max_lat, min_lng, max_lng = _route_bbox(coords)

    cos_lat = math.cos(mean_lat_rad(coords))
    lat_pad = city_mi / MI_PER_DEGREE_LAT
    # Pad by the MAX corridor width so no in-corridor station is
    # pre-excluded before the precise perpendicular test; guard cos_lat
    # near 0 (never divide by ~0).
    lng_pad = city_mi / (MI_PER_DEGREE_LAT * _as_decimal(max(abs(cos_lat), 0.01)))

    stations = list(
        Station.objects.routable().filter(
            latitude__range=(min_lat - lat_pad, max_lat + lat_pad),
            longitude__range=(min_lng - lng_pad, max_lng + lng_pad),
        )
    )

    planar_route = build_planar_route(coords)
    route_length_mi = _as_decimal(planar_route.length)
    total_route_mi = route.total_route_mi

    result = []
    for station in stations:
        planar_point = project_point(
            float(station.longitude), float(station.latitude), coords
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
