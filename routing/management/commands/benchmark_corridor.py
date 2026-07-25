"""Corridor filter before/after benchmark: the legacy per-request DB bbox
query vs. the current lazily-built STRtree, with the mean-latitude
hoisting fix's contribution attributed separately from the tree's.

Also supports a `--profile` mode (CORR-02) that reimplements
`corridor._candidates_single_leg` step-by-step with `time.perf_counter()`
boundaries around five named sub-stages (`buffer`, `tree_query`,
`planar_build`, `vectorized`, `candidate_loop`), reporting each stage's
median time and share of the pass, plus the prefilter survivor count and
final candidate count. The profiled variant is correctness-guarded the
same way the legacy/hoisted/strtree variants above already are: it must
return the exact same candidate set as `corridor.candidates()` (same
sorted `opis_id` list and the same `distance_from_start_mi` strings), or
the command raises `CommandError` -- a profile that measures different
work than production measures nothing.

Read-only reporting command: no writes, no network calls. Routes are
synthesized offline by linearly interpolating (plus a small deterministic
wiggle) between hardcoded continental-US endpoint pairs and densifying to
`--points` geometry vertices -- Mapbox is never touched. Runs against
whatever Station rows are already seeded in the configured database.

Must NOT run in CI: timing numbers are informational, not a pass/fail
gate -- see routing/tests/test_corridor.py for the correctness
coverage this command deliberately does not duplicate.
"""
import json
import math
import statistics
import time
from decimal import Decimal

import shapely
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


_PROFILE_STAGES = ("buffer", "tree_query", "planar_build", "vectorized", "candidate_loop")


def _profiled_single_pass(route):
    """One profiled pass of the single-leg corridor build -- mirrors
    `corridor._candidates_single_leg` step for step, with
    `time.perf_counter()` boundaries around five named sub-stages.
    Returns `(candidates, stage_ms, survivor_count)`; `stage_ms` covers
    THIS pass only -- the caller takes the median across repeats.

    Deliberately duplicates `corridor._candidates_single_leg`'s body
    rather than importing timing hooks into it, following this file's
    existing `_legacy_candidates` precedent (a benchmark-only
    reimplementation, correctness-guarded against the real function by
    the caller comparing candidate sets).
    """
    rooftop_mi, city_mi = corridor._corridor_widths()
    coords = route.raw_coordinates
    mean_lat = corridor.mean_lat_rad(coords)
    tree, indexed_stations = corridor._get_index()

    stage_ms = {}

    # `buffer`: LineString(coords) + .buffer(buffer_deg) against the RAW
    # (unsimplified) route -- corridor.py:302-307's comment records that
    # this buffer always runs against the raw geometry, never the
    # simplified planar line.
    started = time.perf_counter()
    buffer_deg = corridor._corridor_buffer_degrees(coords, city_mi, mean_lat=mean_lat)
    raw_route = LineString(coords)
    query_region = raw_route.buffer(buffer_deg)
    stage_ms["buffer"] = (time.perf_counter() - started) * 1000

    # `tree_query`: the STRtree prefilter query itself.
    started = time.perf_counter()
    survivor_idx = tree.query(query_region, predicate="intersects")
    stations = [indexed_stations[i] for i in survivor_idx]
    stage_ms["tree_query"] = (time.perf_counter() - started) * 1000
    survivor_count = len(stations)

    # `planar_build`: the equirectangular-projected route line plus its
    # simplification -- route_length_mi is derived from this SAME
    # simplified object immediately after, matching corridor.py:311-317's
    # route_length_mi invariant.
    started = time.perf_counter()
    planar_route = corridor.build_planar_route(coords, mean_lat=mean_lat)
    planar_route = planar_route.simplify(corridor.SIMPLIFY_TOLERANCE_MI)
    route_length_mi = corridor._as_decimal(planar_route.length)
    stage_ms["planar_build"] = (time.perf_counter() - started) * 1000

    total_route_mi = route.total_route_mi

    # `vectorized`: the vectorized shapely distance/along-line pass over
    # every prefilter survivor.
    started = time.perf_counter()
    planar_points = corridor._planar_points_for_stations(stations, mean_lat)
    perpendicular_mi_raw = shapely.distance(planar_route, planar_points)
    along_line_mi_raw = shapely.line_locate_point(planar_route, planar_points)
    stage_ms["vectorized"] = (time.perf_counter() - started) * 1000

    # `candidate_loop`: the per-survivor Python loop with its Decimal
    # arithmetic -- byte-identical to corridor._candidates_single_leg's
    # own loop body, including the neutral 1.0 EIA factor (this profile
    # never threads a real factor_for through, matching the production
    # default caller).
    started = time.perf_counter()
    result = []
    for station, perp_raw, along_raw in zip(
        stations, perpendicular_mi_raw, along_line_mi_raw
    ):
        half_width = (
            rooftop_mi
            if station.geocode_precision == GeocodePrecision.ROOFTOP
            else city_mi
        )
        perpendicular_mi = corridor._as_decimal(float(perp_raw))
        if perpendicular_mi > half_width:
            continue

        fraction = corridor._as_decimal(float(along_raw)) / route_length_mi
        distance_from_start_mi = fraction * total_route_mi
        distance_from_start_mi = max(
            Decimal(0), min(distance_from_start_mi, total_route_mi)
        )

        result.append(
            Candidate(
                name=station.name,
                opis_id=station.opis_id,
                price_per_gallon=station.retail_price * corridor._no_op_factor(station.state),
                distance_from_start_mi=distance_from_start_mi,
            )
        )
    stage_ms["candidate_loop"] = (time.perf_counter() - started) * 1000

    return result, stage_ms, survivor_count


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
        "With --profile, additionally splits the current STRtree pass "
        "itself into five named sub-stages (buffer, tree_query, "
        "planar_build, vectorized, candidate_loop) with per-stage median "
        "ms and percent-of-pass, guarded against divergence from "
        "corridor.candidates()'s own output. "
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
        parser.add_argument(
            "--baseline-out",
            type=str,
            default=None,
            help=(
                "Optional path to write a JSON baseline document: per synthetic "
                "route, the sorted candidate opis_id list, each candidate's "
                "distance_from_start_mi (as a string), and the timing medians "
                "already reported. Read-only w.r.t. the DB -- only ever writes "
                "the given path."
            ),
        )
        parser.add_argument(
            "--profile",
            action="store_true",
            help=(
                "Additionally split the current STRtree corridor pass into "
                "five named sub-stages (buffer, tree_query, planar_build, "
                "vectorized, candidate_loop) with per-stage median ms and "
                "percent-of-pass, plus the prefilter survivor count and "
                "final candidate count. Raises CommandError if the profiled "
                "pass ever diverges from corridor.candidates()'s own output."
            ),
        )

    def handle(self, *args, **options):
        n_routes = options["routes"]
        n_points = options["points"]
        repeats = options["repeats"]
        baseline_out = options["baseline_out"]
        profile = options["profile"]

        if n_routes < 1 or n_points < 2 or repeats < 1:
            raise CommandError(
                "--routes and --repeats must each be >= 1, --points must be >= 2"
            )

        pairs = [_ENDPOINT_PAIRS[i % len(_ENDPOINT_PAIRS)] for i in range(n_routes)]

        legacy_ms_all, hoisted_ms_all, strtree_ms_all = [], [], []
        baseline_routes = []

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

            if profile:
                self._run_profile(route, idx, n_points, repeats, strtree_result)

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

            if baseline_out:
                # Recorded off the current production path (`strtree_result`,
                # i.e. `corridor.candidates(route)`) -- the exact answer this
                # plan's later optimization tasks must reproduce byte-for-byte.
                by_opis_id = {c.opis_id: c for c in strtree_result}
                baseline_routes.append(
                    {
                        "index": idx,
                        "endpoint_pair": f"{start} -> {finish}",
                        "points": n_points,
                        "candidates": [
                            {
                                "opis_id": opis_id,
                                "distance_from_start_mi": str(
                                    by_opis_id[opis_id].distance_from_start_mi
                                ),
                            }
                            for opis_id in strtree_ids
                        ],
                        "timings_ms": {
                            "legacy": legacy_ms,
                            "hoisted": hoisted_ms,
                            "strtree": strtree_ms,
                        },
                    }
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

        if baseline_out:
            document = {
                "routes": baseline_routes,
                "overall_medians_ms": {
                    "legacy": overall_legacy,
                    "hoisted": overall_hoisted,
                    "strtree": overall_strtree,
                },
            }
            with open(baseline_out, "w", encoding="utf-8") as f:
                json.dump(document, f, indent=2)
            self.stdout.write(f"Baseline written to {baseline_out}")

    def _run_profile(self, route, idx, n_points, repeats, strtree_result):
        """Run `_profiled_single_pass` `repeats` times for `route`, print a
        per-stage median-ms / percent-of-pass report, and raise
        `CommandError` if the profiled candidate set ever diverges from
        `strtree_result` (the same `corridor.candidates(route)` output
        already timed and correctness-checked by the caller) -- same
        sorted `opis_id` list AND the same `distance_from_start_mi`
        strings. The index is already warm by the time this runs (the
        caller's own STRtree timing above already forced a build), so
        every profiled pass here measures geometry work only.
        """
        stage_samples = {stage: [] for stage in _PROFILE_STAGES}
        survivor_count = None
        profiled_result = None
        for _ in range(repeats):
            profiled_result, stage_ms, survivor_count = _profiled_single_pass(route)
            for stage in _PROFILE_STAGES:
                stage_samples[stage].append(stage_ms[stage])

        profiled_ids = sorted(c.opis_id for c in profiled_result)
        strtree_ids = sorted(c.opis_id for c in strtree_result)
        if profiled_ids != strtree_ids:
            raise CommandError(
                f"Route {idx}: --profile's candidate set diverges from "
                f"corridor.candidates() by opis_id "
                f"(profiled={len(profiled_ids)}, production={len(strtree_ids)}) -- "
                "a profile that measures different work than production "
                "measures nothing."
            )
        profiled_by_id = {c.opis_id: c for c in profiled_result}
        strtree_by_id = {c.opis_id: c for c in strtree_result}
        for opis_id in strtree_ids:
            profiled_distance = str(profiled_by_id[opis_id].distance_from_start_mi)
            production_distance = str(strtree_by_id[opis_id].distance_from_start_mi)
            if profiled_distance != production_distance:
                raise CommandError(
                    f"Route {idx}: --profile's distance_from_start_mi for "
                    f"opis_id={opis_id} diverges from corridor.candidates() "
                    f"({profiled_distance!r} != {production_distance!r})."
                )

        stage_medians = {
            stage: statistics.median(samples) for stage, samples in stage_samples.items()
        }
        pass_total_ms = sum(stage_medians.values())

        self.stdout.write(
            f"Route {idx} ({n_points} pts) --profile: "
            f"{survivor_count} prefilter survivor(s) -> "
            f"{len(profiled_result)} candidate(s), "
            f"pass total (sum of stage medians)={pass_total_ms:.2f}ms"
        )
        for stage in _PROFILE_STAGES:
            median_ms = stage_medians[stage]
            percent = (median_ms / pass_total_ms * 100) if pass_total_ms > 0 else 0.0
            self.stdout.write(f"    {stage:<15} median={median_ms:8.3f}ms  ({percent:5.1f}%)")
