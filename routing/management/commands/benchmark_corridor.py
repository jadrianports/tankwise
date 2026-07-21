"""Corridor filter before/after benchmark: the legacy per-request DB bbox
query vs. the current lazily-built STRtree, with the mean-latitude
hoisting fix's contribution attributed separately from the tree's.

Read-only reporting command: no writes, no network calls. Routes are
synthesized offline by linearly interpolating (plus a small deterministic
wiggle) between hardcoded continental-US endpoint pairs and densifying to
`--points` geometry vertices -- Mapbox is never touched. Runs against
whatever Station rows are already seeded in the configured database.

Must NOT run in CI: timing numbers are informational, not a pass/fail
gate -- see routing/tests/test_corridor.py for the correctness
coverage this command deliberately does not duplicate.
"""
import math
import statistics
import time
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from shapely.geometry import LineString

from routing.models import GeocodePrecision, Station
from routing.services import corridor
from routing.services.mapbox import Route
from routing.services.solver import Candidate

# lng, lat endpoint pairs spread across the continental US so each
# synthetic route's mean latitude (and therefore its buffer anisotropy)
# differs -- offline stand-ins for a real Mapbox Directions response.
_ENDPOINT_PAIRS = [
    ((-74.0060, 40.7128), (-118.2437, 34.0522)),  # New York -> Los Angeles
    ((-87.6298, 41.8781), (-95.3698, 29.7604)),  # Chicago -> Houston
    ((-122.3321, 47.6062), (-80.1918, 25.7617)),  # Seattle -> Miami
]


def _synthetic_route(start, finish, points):
    """A deterministic, densified route between two endpoints: a straight
    interpolation with a small sinusoidal wiggle so the geometry is not a
    degenerate single line segment (matches the wiggle idiom already used
    in routing/tests/test_views.py's _dense_long_directions_payload)."""
    (start_lng, start_lat), (finish_lng, finish_lat) = start, finish
    coords = []
    for i in range(points):
        t = i / (points - 1)
        lng = start_lng + (finish_lng - start_lng) * t
        lat = start_lat + (finish_lat - start_lat) * t
        lat += 0.3 * math.sin(t * 30) + 0.05 * math.sin(t * 137)
        coords.append((lng, lat))

    planar_route = corridor.build_planar_route(coords)
    total_route_mi = corridor._as_decimal(planar_route.length)
    return Route(
        total_route_mi=total_route_mi,
        geometry=LineString(coords),
        raw_coordinates=coords,
    )


def _legacy_candidates(route, *, hoist_mean_lat):
    """Benchmark-only historical reference of the legacy corridor
    path: a per-request DB bbox query followed by the precise perpendicular
    test. The production `candidates()` in routing.services.corridor no
    longer contains this code path -- it now queries the STRtree instead.

    `hoist_mean_lat` toggles whether mean_lat_rad(coords) is computed once
    and threaded through (the mean-latitude hoisting fix) or re-derived on
    every project_point()/build_planar_route() call (the pre-fix behavior)
    -- this isolates the hoisting win from the STRtree win.
    """
    rooftop_mi, city_mi = corridor._corridor_widths()
    coords = route.raw_coordinates

    mean_lat = corridor.mean_lat_rad(coords)
    cos_lat = math.cos(mean_lat)
    lat_pad = city_mi / corridor.MI_PER_DEGREE_LAT
    lng_pad = city_mi / (
        corridor.MI_PER_DEGREE_LAT * corridor._as_decimal(max(abs(cos_lat), 0.01))
    )

    min_lat, max_lat, min_lng, max_lng = corridor._route_bbox(coords)
    stations = list(
        Station.objects.routable().filter(
            latitude__range=(min_lat - lat_pad, max_lat + lat_pad),
            longitude__range=(min_lng - lng_pad, max_lng + lng_pad),
        )
    )

    if hoist_mean_lat:
        planar_route = corridor.build_planar_route(coords, mean_lat=mean_lat)
    else:
        planar_route = corridor.build_planar_route(coords)
    route_length_mi = corridor._as_decimal(planar_route.length)
    total_route_mi = route.total_route_mi

    result = []
    for station in stations:
        if hoist_mean_lat:
            planar_point = corridor.project_point(
                float(station.longitude),
                float(station.latitude),
                coords,
                mean_lat=mean_lat,
            )
        else:
            planar_point = corridor.project_point(
                float(station.longitude), float(station.latitude), coords
            )

        half_width = (
            rooftop_mi
            if station.geocode_precision == GeocodePrecision.ROOFTOP
            else city_mi
        )
        perpendicular_mi = corridor._as_decimal(planar_route.distance(planar_point))
        if perpendicular_mi > half_width:
            continue

        fraction = (
            corridor._as_decimal(planar_route.project(planar_point)) / route_length_mi
        )
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


def _median_ms(fn, repeats):
    samples_ms = []
    result = None
    for _ in range(repeats):
        started = time.perf_counter()
        result = fn()
        samples_ms.append((time.perf_counter() - started) * 1000)
    return statistics.median(samples_ms), result


class Command(BaseCommand):
    help = (
        "Time the corridor filter's legacy DB-bbox path against the "
        "current STRtree path over synthetic offline routes, attributing "
        "the mean-latitude-hoisting and STRtree speedups separately. "
        "Read-only: no writes, no network calls. Must NOT run in CI -- "
        "timing numbers are informational, not a pass/fail gate."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--routes",
            type=int,
            default=3,
            help="Number of synthetic routes to time (default 3).",
        )
        parser.add_argument(
            "--points",
            type=int,
            default=2000,
            help="Geometry points per synthetic route (default 2000).",
        )
        parser.add_argument(
            "--repeats",
            type=int,
            default=5,
            help="Timing repetitions per variant, per route (default 5).",
        )

    def handle(self, *args, **options):
        n_routes = options["routes"]
        n_points = options["points"]
        repeats = options["repeats"]

        if n_routes < 1 or n_points < 2 or repeats < 1:
            raise CommandError(
                "--routes and --repeats must each be >= 1, --points must be >= 2"
            )

        pairs = [_ENDPOINT_PAIRS[i % len(_ENDPOINT_PAIRS)] for i in range(n_routes)]

        legacy_ms_all, hoisted_ms_all, strtree_ms_all = [], [], []

        for idx, (start, finish) in enumerate(pairs, start=1):
            route = _synthetic_route(start, finish, n_points)

            legacy_ms, legacy_result = _median_ms(
                lambda: _legacy_candidates(route, hoist_mean_lat=False), repeats
            )
            hoisted_ms, hoisted_result = _median_ms(
                lambda: _legacy_candidates(route, hoist_mean_lat=True), repeats
            )

            # Reset, then one UNTIMED warm-up call so the reported STRtree
            # number excludes the one-time tree-build cost -- matching the
            # production shape ("DB-free after first use"), not the
            # cold-start cost. Labelled explicitly below.
            corridor.reset_index()
            corridor.candidates(route)
            strtree_ms, strtree_result = _median_ms(
                lambda: corridor.candidates(route), repeats
            )

            legacy_ids = sorted(c.opis_id for c in legacy_result)
            hoisted_ids = sorted(c.opis_id for c in hoisted_result)
            strtree_ids = sorted(c.opis_id for c in strtree_result)

            if not (legacy_ids == hoisted_ids == strtree_ids):
                self.stdout.write(
                    self.style.WARNING(
                        f"Route {idx}: candidate sets diverge across variants "
                        f"(legacy={len(legacy_ids)}, "
                        f"legacy+hoisted={len(hoisted_ids)}, "
                        f"strtree={len(strtree_ids)}) -- a faster path that "
                        "returns a different answer is a bug, not a speedup."
                    )
                )
                raise CommandError(
                    f"Route {idx}: the three corridor variants returned "
                    "different candidate sets."
                )

            legacy_ms_all.append(legacy_ms)
            hoisted_ms_all.append(hoisted_ms)
            strtree_ms_all.append(strtree_ms)

            hoist_speedup = legacy_ms / hoisted_ms if hoisted_ms > 0 else float("inf")
            tree_speedup = hoisted_ms / strtree_ms if strtree_ms > 0 else float("inf")

            self.stdout.write(
                f"Route {idx} ({n_points} pts, {len(strtree_ids)} candidates): "
                f"legacy bbox={legacy_ms:.2f}ms | "
                f"legacy bbox + hoisted mean_lat={hoisted_ms:.2f}ms "
                f"({hoist_speedup:.2f}x) | "
                f"STRtree + hoisted mean_lat, warm={strtree_ms:.2f}ms "
                f"({tree_speedup:.2f}x over hoisted-only)"
            )

        overall_legacy = statistics.median(legacy_ms_all)
        overall_hoisted = statistics.median(hoisted_ms_all)
        overall_strtree = statistics.median(strtree_ms_all)
        overall_hoist_speedup = (
            overall_legacy / overall_hoisted if overall_hoisted > 0 else float("inf")
        )
        overall_tree_speedup = (
            overall_hoisted / overall_strtree if overall_strtree > 0 else float("inf")
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Median over {n_routes} route(s), {repeats} repeat(s) each: "
                f"legacy bbox={overall_legacy:.2f}ms -> "
                f"+ hoisted mean_lat={overall_hoisted:.2f}ms "
                f"({overall_hoist_speedup:.2f}x) -> "
                f"+ STRtree (warm)={overall_strtree:.2f}ms "
                f"({overall_tree_speedup:.2f}x over hoisted-only)"
            )
        )
