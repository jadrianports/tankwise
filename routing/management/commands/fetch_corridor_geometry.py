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
"""
import json

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

# Pre-decided per D-15/RESEARCH.md Open Questions item 1: if a single
# trimmed fixture exceeds this, stop and report a finding rather than
# fetching the rest or degrading fidelity. 1,500,000 bytes projects to
# ~18 MB for twelve fixtures -- large but not the point at which the
# response should be simplified/downsampled (D-15's hard constraint
# forbids that outright regardless of size).
SIZE_FINDING_THRESHOLD_BYTES = 1_500_000


class Command(BaseCommand):
    help = (
        "One-time, ONLINE capture of real Mapbox Directions geometry for "
        "the twelve pinned measurement corridors (routing.tests."
        "test_corridor_fixtures.CORRIDORS). Writes trimmed, byte-faithful "
        "JSON fixtures under routing/tests/fixtures/corridor_geometry/ so "
        "every later reduction figure can be re-derived offline, with no "
        "Mapbox token and no network. This command must NOT run in CI -- "
        "routing.tests.test_corridor_fixtures.CorridorFixtureTests is the "
        "actual CI-side guard that the committed fixtures stay valid."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--corridor",
            action="append",
            default=None,
            help=(
                "Corridor slug to capture (repeatable, or comma-separated "
                "within one value). Default: all twelve pinned corridors."
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

    def handle(self, *args, **options):
        slugs = self._resolve_slugs(options["corridor"])
        force = options["force"]
        no_budget = options["no_budget"]

        CORRIDOR_GEOMETRY_DIR.mkdir(parents=True, exist_ok=True)

        captured = []
        for slug in slugs:
            corridor = _CORRIDOR_BY_SLUG[slug]
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

    def _resolve_slugs(self, corridor_option):
        if not corridor_option:
            return [corridor.slug for corridor in CORRIDORS]

        requested = []
        for value in corridor_option:
            requested.extend(part.strip() for part in value.split(",") if part.strip())

        unknown = [slug for slug in requested if slug not in _CORRIDOR_BY_SLUG]
        if unknown:
            raise CommandError(
                f"Unknown corridor slug(s): {unknown}. Valid slugs: "
                f"{[c.slug for c in CORRIDORS]}"
            )
        return requested

    def _capture_one(self, corridor, *, force, no_budget):
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

        # Coordinate path built lng,lat joined by ';', start then finish --
        # the same construction get_routes() performs. corridor.start and
        # corridor.finish are (lat, lng) pairs (see Corridor's docstring).
        coords_path = ";".join(
            f"{lng},{lat}" for lat, lng in (corridor.start, corridor.finish)
        )
        url = f"{DIRECTIONS_BASE_URL}/{coords_path}"

        # Parameter dict identical to get_routes()'s. This transport call
        # deliberately duplicates get_routes()'s own GET, ONLY here,
        # because get_routes() discards the raw JSON body (it returns
        # parsed Route objects) and this command needs the raw body to
        # build a committed fixture.
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
        trimmed = {
            "code": code,
            "routes": [
                {
                    "distance": route_data["distance"],
                    "duration": route_data["duration"],
                    "geometry": {
                        "coordinates": route_data["geometry"]["coordinates"]
                    },
                    "legs": (
                        [{"distance": legs_data[0]["distance"]}] if legs_data else []
                    ),
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
        # test_corridor_fixtures.load_corridor_route() will, and confirm
        # it reproduces production's own parser output byte-for-byte.
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
        self.stdout.write(
            f"{corridor.slug}: {coord_count} coordinates, "
            f"total_route_mi={parsed_route.total_route_mi:.1f} "
            f"(pinned estimate {corridor.estimated_driving_mi} mi), "
            f"{byte_size} bytes"
        )

        return {
            "slug": corridor.slug,
            "coord_count": coord_count,
            "total_route_mi": parsed_route.total_route_mi,
            "byte_size": byte_size,
        }
