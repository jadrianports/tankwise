"""One-time, ONLINE capture of real Mapbox Directions geometry per corridor.

Makes live Mapbox Directions calls -- the only networked artifact in Phase
17. Everything downstream (the offline corridor filter, the domination
prune, and plan 17-05's `measure_prune_reduction` report) replays this
command's committed output offline, with no network access and no Mapbox
token, forever.

Must NOT run in CI. This command exists so the geometry behind every
reduction figure is captured reproducibly, rather than by a one-off script
that later disappears -- which is precisely what happened to the harness
behind the lost 508/121 and 157/286 figures (D-15/D-16). The CI-side guard
that these committed fixtures stay valid is
`routing.tests.test_corridor_fixtures.CorridorFixtureTests`, not this
command.

Read-only w.r.t. the database: writes only the fixture JSON files under
`routing/tests/fixtures/corridor_geometry/`.

Phase 18.1 (D-13) adds a waypoint-aware capture mode alongside the original
single-leg one. `--waypoints` builds a genuinely multi-leg fixture -- one
Mapbox leg per hop -- so the demo chips' multi-stop route is consumed
through `multi_leg.flatten_route`'s existing per-leg-LineString path, the
exact code production runs, rather than a merged single-leg approximation.
The single-leg branch below is byte-for-byte unchanged; none of the twelve
existing fixtures are re-captured by this change.
"""
import json
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from routing.services import budget
from routing.services.budget import UpstreamBudgetExhaustedError
from routing.services.mapbox import (
    DIRECTIONS_BASE_URL,
    _SESSION,
    _parse_directions_response,
)
from routing.tests.test_corridor_fixtures import CORRIDOR_GEOMETRY_DIR, CORRIDORS

_CORRIDOR_BY_SLUG = {corridor.slug: corridor for corridor in CORRIDORS}

# Demo chips (D-13/D-14) are registered by plan 18.1-03 Task 2 in the same
# module CORRIDORS lives in. Imported defensively: this command's --help
# and its non-waypoint acceptance criteria must keep working before Task 2
# lands DEMO_CHIPS, and every capture invocation this command's docstring
# and Task 3's checkpoint describe runs only after both tasks are committed.
try:
    from routing.tests.test_corridor_fixtures import DEMO_CHIPS
except ImportError:
    DEMO_CHIPS = ()

_TARGET_BY_SLUG = dict(_CORRIDOR_BY_SLUG)
_TARGET_BY_SLUG.update({chip.slug: chip for chip in DEMO_CHIPS})

# Pre-decided per D-15/RESEARCH.md Open Questions item 1: if a single
# trimmed fixture exceeds this, stop and report a finding rather than
# fetching the rest or degrading fidelity. 1,500,000 bytes projects to
# ~18 MB for twelve fixtures -- large but not the point at which the
# response should be simplified/downsampled (D-15's hard constraint
# forbids that outright regardless of size). Reused unchanged for the two
# demo-chip captures (Phase 18.1 CONTEXT.md "Fixture size policy").
SIZE_FINDING_THRESHOLD_BYTES = 1_500_000


class _AdHocTarget:
    """A capture target built from `--slug`/`--start`/`--finish`, not from
    the committed `CORRIDORS`/`DEMO_CHIPS` registries. Mirrors the
    `slug`/`start`/`finish`/`estimated_driving_mi` shape both registries
    already expose so `_capture_one` needs no branching on target type."""

    def __init__(self, slug, start, finish):
        self.slug = slug
        self.start = start
        self.finish = finish
        self.estimated_driving_mi = None


class Command(BaseCommand):
    help = (
        "One-time, ONLINE capture of real Mapbox Directions geometry for "
        "the twelve pinned measurement corridors (routing.tests."
        "test_corridor_fixtures.CORRIDORS) and, via --waypoints, an "
        "ad-hoc or demo-chip multi-leg route. Writes trimmed, "
        "byte-faithful JSON fixtures under "
        "routing/tests/fixtures/corridor_geometry/ so every later figure "
        "can be re-derived offline, with no Mapbox token and no network. "
        "This command must NOT run in CI -- "
        "routing.tests.test_corridor_fixtures.CorridorFixtureTests and "
        "DemoChipFixtureTests are the actual CI-side guards that the "
        "committed fixtures stay valid."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--corridor",
            action="append",
            default=None,
            help=(
                "Corridor or demo-chip slug to capture (repeatable, or "
                "comma-separated within one value). Default: all twelve "
                "pinned corridors. With --waypoints, exactly one slug must "
                "be given."
            ),
        )
        parser.add_argument(
            "--force",
            action="store_true",
            default=False,
            help="Overwrite an existing fixture. Default: a second run of "
            "an already-captured corridor is a no-op.",
        )
        parser.add_argument(
            "--no-budget",
            action="store_true",
            default=False,
            help="Escape hatch: skip budget.consume() for this capture. "
            "Prints a loud warning that budget accounting was bypassed. "
            "Default: goes through the existing budget module like every "
            "other outbound Mapbox call.",
        )
        parser.add_argument(
            "--waypoints",
            action="append",
            default=None,
            help=(
                "Intermediate waypoint as 'lat,lng' (repeatable, given in "
                "visit order). Builds a genuinely multi-leg capture "
                "through the waypoint-aware branch (D-13): one Mapbox leg "
                "per hop, retaining every leg's distance and annotation "
                "arrays, not just leg 0's. Only valid together with "
                "exactly one --corridor slug, or with --slug plus "
                "explicit --start/--finish; any other combination raises "
                "a CommandError. Absent, capture behaviour is "
                "byte-identical to the existing single-leg path."
            ),
        )
        parser.add_argument(
            "--slug",
            default=None,
            help=(
                "Fixture slug to write for an ad-hoc waypointed capture, "
                "used together with --start/--finish (not --corridor)."
            ),
        )
        parser.add_argument(
            "--start",
            default=None,
            help="Ad-hoc capture start coordinate 'lat,lng' (used with --slug).",
        )
        parser.add_argument(
            "--finish",
            default=None,
            help="Ad-hoc capture finish coordinate 'lat,lng' (used with --slug).",
        )

    def handle(self, *args, **options):
        force = options["force"]
        no_budget = options["no_budget"]
        waypoints_raw = options["waypoints"]
        slug_option = options["slug"]
        start_option = options["start"]
        finish_option = options["finish"]
        corridor_option = options["corridor"]

        CORRIDOR_GEOMETRY_DIR.mkdir(parents=True, exist_ok=True)

        if waypoints_raw:
            waypoints = [self._parse_point(value) for value in waypoints_raw]
            ad_hoc_fields_given = bool(slug_option or start_option or finish_option)
            via_corridor = bool(corridor_option)

            if ad_hoc_fields_given and via_corridor:
                raise CommandError(
                    "--waypoints cannot combine --corridor with "
                    "--slug/--start/--finish -- choose exactly one capture "
                    "target."
                )
            if ad_hoc_fields_given:
                if not (slug_option and start_option and finish_option):
                    raise CommandError(
                        "--waypoints with an ad-hoc capture requires "
                        "--slug, --start and --finish all set."
                    )
                target = _AdHocTarget(
                    slug_option,
                    self._parse_point(start_option),
                    self._parse_point(finish_option),
                )
            elif via_corridor:
                slugs = self._resolve_slugs(corridor_option)
                if len(slugs) != 1:
                    raise CommandError(
                        "--waypoints requires exactly one --corridor slug "
                        f"(got {len(slugs)}: {slugs})."
                    )
                target = _TARGET_BY_SLUG[slugs[0]]
            else:
                raise CommandError(
                    "--waypoints requires either a single --corridor slug "
                    "(naming a demo chip) or --slug plus --start/--finish "
                    "for an ad-hoc capture."
                )

            result = self._capture_one(
                target, force=force, no_budget=no_budget, waypoints=waypoints
            )
            captured = [result] if result is not None else []
            total_bytes = sum(entry["byte_size"] for entry in captured)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Captured {len(captured)}/1 requested waypointed "
                    f"route(s), {total_bytes} total bytes."
                )
            )
            return

        slugs = self._resolve_slugs(corridor_option)

        captured = []
        for slug in slugs:
            corridor = _TARGET_BY_SLUG[slug]
            result = self._capture_one(corridor, force=force, no_budget=no_budget)
            if result is not None:
                captured.append(result)

        total_bytes = sum(entry["byte_size"] for entry in captured)
        self.stdout.write(
            self.style.SUCCESS(
                f"Captured {len(captured)}/{len(slugs)} requested corridor(s), "
                f"{total_bytes} total bytes."
            )
        )

    def _parse_point(self, value):
        try:
            lat_str, lng_str = value.split(",")
            return (Decimal(lat_str.strip()), Decimal(lng_str.strip()))
        except (ValueError, InvalidOperation) as exc:
            raise CommandError(
                f"Invalid coordinate {value!r}, expected 'lat,lng'"
            ) from exc

    def _resolve_slugs(self, corridor_option):
        if not corridor_option:
            return [corridor.slug for corridor in CORRIDORS]

        requested = []
        for value in corridor_option:
            requested.extend(part.strip() for part in value.split(",") if part.strip())

        unknown = [slug for slug in requested if slug not in _TARGET_BY_SLUG]
        if unknown:
            raise CommandError(
                f"Unknown corridor/demo-chip slug(s): {unknown}. Valid "
                f"slugs: {list(_TARGET_BY_SLUG)}"
            )
        return requested

    def _capture_one(self, corridor, *, force, no_budget, waypoints=None):
        fixture_path = CORRIDOR_GEOMETRY_DIR / f"{corridor.slug}.json"
        if fixture_path.exists() and not force:
            self.stdout.write(
                f"{corridor.slug}: already captured at {fixture_path} "
                "(pass --force to overwrite)"
            )
            return None

        if not settings.MAPBOX_TOKEN:
            raise CommandError(
                "MAPBOX_TOKEN is not set -- cannot call the Mapbox "
                "Directions API. This is the existing server-side sk "
                "token already required by "
                "routing.services.mapbox.get_routes(), not the public pk "
                "token used for map_url."
            )

        if no_budget:
            self.stdout.write(
                self.style.WARNING(
                    f"{corridor.slug}: --no-budget set -- budget accounting "
                    "was bypassed for this one-time capture."
                )
            )
        else:
            try:
                budget.consume(budget.DIRECTIONS)
            except UpstreamBudgetExhaustedError as exc:
                raise CommandError(
                    f"{corridor.slug}: Directions budget exhausted: {exc}"
                ) from exc

        # Coordinate path built lng,lat joined by ';', start then finish
        # (D-13, waypointed: start, then every waypoint in the given
        # order, then finish -- never sorted, never deduped, matching
        # routing/cache.py's own ordered-chain rule). corridor.start and
        # corridor.finish are (lat, lng) pairs (see Corridor's docstring).
        stops = (corridor.start, *(waypoints or ()), corridor.finish)
        coords_path = ";".join(f"{lng},{lat}" for lat, lng in stops)
        url = f"{DIRECTIONS_BASE_URL}/{coords_path}"

        # Parameter dict identical to get_routes()'s, waypointed or not.
        # This transport call deliberately duplicates get_routes()'s own
        # GET, ONLY here, because get_routes() discards the raw JSON body
        # (it returns parsed Route objects) and this command needs the raw
        # body to build a committed fixture.
        try:
            response = _SESSION.get(
                url,
                params={
                    "geometries": "geojson",
                    "overview": "full",
                    "alternatives": "true",
                    "annotations": "duration,distance",
                    "access_token": settings.MAPBOX_TOKEN,
                },
                timeout=10,
            )
        except Exception as exc:  # noqa: BLE001 - any transport failure is fatal here
            raise CommandError(
                f"{corridor.slug}: Mapbox Directions request failed: {exc}"
            ) from exc

        if response.status_code != 200:
            raise CommandError(
                f"{corridor.slug}: Mapbox Directions request failed with "
                f"status {response.status_code}"
            )

        body = response.json()
        code = body.get("code")
        if code != "Ok" or not body.get("routes"):
            raise CommandError(
                f"{corridor.slug}: Mapbox returned code={code!r} with no "
                "usable route"
            )

        route_data = body["routes"][0]
        # `duration` is required, not optional -- mapbox._parse_single_route
        # reads route_data["duration"] by direct subscript and raises
        # KeyError without it. Every other field it reads is defensive
        # (`.get(..., default)`), which is what makes the rest of this trim
        # safe: the annotation arrays this drops are never read by
        # corridor.candidates(). Coordinates are copied element-for-element
        # -- never simplified, downsampled, rounded, or re-encoded (D-15's
        # hard fidelity constraint; RESEARCH.md Pitfall 3).
        legs_data = route_data.get("legs") or []
        if waypoints:
            # Waypoint branch (D-13): retain EVERY leg's distance and its
            # annotation.distance/duration arrays. Unlike the single-leg
            # trim below, `multi_leg.flatten_route`'s consumer path slices
            # the combined coordinate array by these exact
            # `leg_annotation_lengths` and builds `leg_boundaries_mi` from
            # every leg's `leg_distances_mi` entry -- dropping any leg's
            # annotation arrays (as the single-leg trim does for legs > 0)
            # silently produces a wrong/zero-length slice downstream.
            trimmed_legs = [
                {
                    "distance": leg["distance"],
                    "annotation": {
                        "distance": leg.get("annotation", {}).get("distance") or [],
                        "duration": leg.get("annotation", {}).get("duration") or [],
                    },
                }
                for leg in legs_data
            ]
        else:
            # Single-leg branch: unchanged from Phase 17. This path never
            # calls flatten_route, so annotation lengths beyond leg 0 are
            # unused here -- only the waypoint branch above needs them.
            trimmed_legs = (
                [{"distance": legs_data[0]["distance"]}] if legs_data else []
            )

        trimmed = {
            "code": code,
            "routes": [
                {
                    "distance": route_data["distance"],
                    "duration": route_data["duration"],
                    "geometry": {
                        "coordinates": route_data["geometry"]["coordinates"]
                    },
                    "legs": trimmed_legs,
                }
            ],
        }

        with open(fixture_path, "w", encoding="utf-8") as f:
            json.dump(trimmed, f)

        byte_size = fixture_path.stat().st_size

        if byte_size > SIZE_FINDING_THRESHOLD_BYTES:
            self.stdout.write(
                self.style.ERROR(
                    f"{corridor.slug}: fixture is {byte_size} bytes, over "
                    f"the {SIZE_FINDING_THRESHOLD_BYTES}-byte pre-decided "
                    "threshold. Per D-15, this is reported as a finding, "
                    "not fixed by simplifying/downsampling the geometry -- "
                    "see this plan's SUMMARY.md."
                )
            )

        # Round-trip guard: re-read the file exactly as
        # test_corridor_fixtures.load_corridor_route()/load_demo_chip_route()
        # will, and confirm it reproduces production's own parser output.
        with open(fixture_path, encoding="utf-8") as f:
            reread = json.load(f)
        parsed_route = _parse_directions_response(reread)[0]

        captured_coords = trimmed["routes"][0]["geometry"]["coordinates"]
        if parsed_route.raw_coordinates != captured_coords:
            raise CommandError(
                f"{corridor.slug}: round-trip guard failed -- "
                "raw_coordinates diverges from the captured coordinate "
                "array"
            )

        if waypoints:
            expected_leg_count = len(waypoints) + 1
            # OPPOSITE assertion from the single-leg branch below: a
            # genuinely multi-leg capture must parse back to more than one
            # leg, and to exactly the leg count the waypoint list implies.
            if len(parsed_route.leg_distances_mi) <= 1:
                raise CommandError(
                    f"{corridor.slug}: round-trip guard failed -- "
                    f"leg_distances_mi has {len(parsed_route.leg_distances_mi)} "
                    "entries, expected >1 for the waypoint-aware branch"
                )
            if len(parsed_route.leg_distances_mi) != expected_leg_count:
                raise CommandError(
                    f"{corridor.slug}: round-trip guard failed -- "
                    f"leg_distances_mi has {len(parsed_route.leg_distances_mi)} "
                    f"entries, expected {expected_leg_count} for "
                    f"{len(waypoints)} waypoint(s)"
                )
            if len(parsed_route.leg_annotation_lengths) != expected_leg_count:
                raise CommandError(
                    f"{corridor.slug}: round-trip guard failed -- "
                    f"leg_annotation_lengths has "
                    f"{len(parsed_route.leg_annotation_lengths)} entries, "
                    f"expected {expected_leg_count}"
                )
            if sum(parsed_route.leg_annotation_lengths) + 1 != len(captured_coords):
                raise CommandError(
                    f"{corridor.slug}: round-trip guard failed -- sum of "
                    "leg_annotation_lengths + 1 "
                    f"({sum(parsed_route.leg_annotation_lengths) + 1}) does "
                    f"not equal the coordinate count ({len(captured_coords)})"
                )
            if not (parsed_route.total_route_mi > 0):
                raise CommandError(
                    f"{corridor.slug}: round-trip guard failed -- "
                    f"total_route_mi={parsed_route.total_route_mi} is not "
                    "positive"
                )

            # The guard that the fixture is actually consumable by the
            # production path, not merely parseable: flatten it exactly as
            # corridor.candidates() does for any 2+-leg route.
            from routing.services.multi_leg import flatten_route

            flattened = flatten_route(parsed_route)
            if len(flattened.leg_lines) != expected_leg_count:
                raise CommandError(
                    f"{corridor.slug}: round-trip guard failed -- "
                    f"flatten_route returned {len(flattened.leg_lines)} "
                    f"line(s), expected {expected_leg_count}"
                )
            boundaries = flattened.leg_boundaries_mi
            if len(boundaries) != expected_leg_count or boundaries[0] != 0:
                raise CommandError(
                    f"{corridor.slug}: round-trip guard failed -- "
                    f"leg_boundaries_mi={boundaries} is not a "
                    f"{expected_leg_count}-entry sequence starting at 0"
                )
            for previous, current in zip(boundaries, boundaries[1:]):
                if not (current > previous):
                    raise CommandError(
                        f"{corridor.slug}: round-trip guard failed -- "
                        f"leg_boundaries_mi={boundaries} is not strictly "
                        "increasing"
                    )
        else:
            if len(parsed_route.leg_distances_mi) > 1:
                raise CommandError(
                    f"{corridor.slug}: round-trip guard failed -- "
                    f"leg_distances_mi has {len(parsed_route.leg_distances_mi)} "
                    "entries, expected <=1 for the single-leg branch"
                )
            if not (parsed_route.total_route_mi > 0):
                raise CommandError(
                    f"{corridor.slug}: round-trip guard failed -- "
                    f"total_route_mi={parsed_route.total_route_mi} is not "
                    "positive"
                )

        coord_count = len(captured_coords)
        estimate_display = (
            f" (pinned estimate {corridor.estimated_driving_mi} mi)"
            if corridor.estimated_driving_mi is not None
            else ""
        )
        self.stdout.write(
            f"{corridor.slug}: {coord_count} coordinates, "
            f"total_route_mi={parsed_route.total_route_mi:.1f}"
            f"{estimate_display}, {byte_size} bytes"
        )

        return {
            "slug": corridor.slug,
            "coord_count": coord_count,
            "total_route_mi": parsed_route.total_route_mi,
            "byte_size": byte_size,
        }
