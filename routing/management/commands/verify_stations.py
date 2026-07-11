"""Opt-in routable-coverage gate over the seeded Station table.

Read-only reporting command: no writes, no network calls. Reports the
routable share of IN-SCOPE stations (pending + ok + failed -- the
geocodable population) alongside the four status-bucket counts, and, when
`--min-coverage` is supplied, raises CommandError (non-zero exit) if the
ratio falls below the bar so Docker build / CI can gate on data quality.

Coverage is deliberately scoped to in-scope stations only: the 620
out_of_scope (non-lower-48) rows are never geocoded and always have null
coordinates, so an "assert zero null coordinates" gate (or an unscoped
denominator) could never pass and would tell you nothing about the
geocoder's actual performance.

The real pipeline run achieved ~94.9% routable coverage (6,290/6,626
in-scope); 0.90 is a sensible regression-catching bar. The flag has no
default -- omitting it reports only; a caller opts in with its bar.
"""

from django.core.management.base import BaseCommand, CommandError

from routing.models import GeocodeStatus, Station


class Command(BaseCommand):
    help = (
        "Report routable-coverage over in-scope Station rows and, when "
        "--min-coverage is given, exit non-zero if coverage falls below it. "
        "Read-only: no writes, no network calls."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--min-coverage",
            type=float,
            default=None,
            help=(
                "Minimum acceptable routable-coverage ratio (0-1) over "
                "in-scope stations. If omitted, report only (always exits 0). "
                "The real pipeline run achieved ~0.949; 0.90 is a reasonable "
                "regression-catching bar for Docker build / CI."
            ),
        )

    def handle(self, *args, **options):
        min_coverage = options["min_coverage"]

        rooftop_count = Station.objects.routable().filter(
            geocode_precision="rooftop"
        ).count()
        city_count = Station.objects.routable().filter(geocode_precision="city").count()
        failed_count = Station.objects.filter(geocode_status=GeocodeStatus.FAILED).count()
        out_of_scope_count = Station.objects.filter(
            geocode_status=GeocodeStatus.OUT_OF_SCOPE
        ).count()

        routable_count = Station.objects.routable().count()
        # Denominator = in-scope (geocodable) population only: pending + ok
        # + failed. out_of_scope rows are excluded from BOTH numerator and
        # denominator so they can never inflate or deflate coverage.
        in_scope_count = Station.objects.exclude(
            geocode_status=GeocodeStatus.OUT_OF_SCOPE
        ).count()

        if in_scope_count == 0:
            coverage = 0.0
        else:
            coverage = routable_count / in_scope_count

        self.stdout.write(
            f"Routable coverage (in-scope): {routable_count}/{in_scope_count} "
            f"({coverage:.4f})"
        )
        self.stdout.write(
            f"Breakdown: rooftop={rooftop_count} city={city_count} "
            f"failed={failed_count} out_of_scope={out_of_scope_count}"
        )

        if min_coverage is None:
            self.stdout.write(
                self.style.WARNING(
                    "No --min-coverage supplied; report only (always exits 0)."
                )
            )
            return

        if coverage < min_coverage:
            raise CommandError(
                f"Routable coverage {coverage:.4f} is below the required "
                f"--min-coverage {min_coverage:.4f} "
                f"({routable_count}/{in_scope_count} in-scope stations routable)"
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Routable coverage {coverage:.4f} meets --min-coverage {min_coverage:.4f}"
            )
        )
