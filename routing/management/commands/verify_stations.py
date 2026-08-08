"""Opt-in routable-coverage gate, plus four UNCONDITIONAL dataset
invariants, over the seeded Station table.

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

D-42 (plan 22-13): four dataset invariants over `source`/`gers_id`/`opis_id`/
`retail_price` run below, alongside the pre-existing `price_source` gate,
ALWAYS -- never gated behind `--min-coverage`, for the same reason that gate
already runs unconditionally: this command sits in the Docker build path,
where a data defect needs to fail the build regardless of whether a coverage
bar was even asked for. This is also the only check in this phase runnable
against a real database, including production after plan 22-16's deploy.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Max, Q

from routing.models import GeocodeStatus, PriceSource, Station, StationSource
from routing.pipeline.overture_scope import OVERTURE_ID_RANGE
from routing.services import regions


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

        opis_indexed_count = Station.objects.filter(
            price_source=PriceSource.OPIS_INDEXED
        ).count()
        eia_regional_estimate_count = Station.objects.filter(
            price_source=PriceSource.EIA_REGIONAL_ESTIMATE
        ).count()
        # Single query so an unknown value's cost is one extra COUNT, not a
        # per-row scan -- a data defect at any coverage level, so this check
        # runs unconditionally (not gated behind --min-coverage) since this
        # command runs in the Docker build path where the gate needs to fire.
        unrecognized_price_source_count = Station.objects.exclude(
            price_source__in=PriceSource.values
        ).count()

        self.stdout.write(
            f"Price source breakdown: opis_indexed={opis_indexed_count} "
            f"eia_regional_estimate={eia_regional_estimate_count} "
            f"unrecognized={unrecognized_price_source_count}"
        )

        if unrecognized_price_source_count:
            raise CommandError(
                f"{unrecognized_price_source_count} station row(s) hold a "
                f"price_source value outside {PriceSource.values}"
            )

        # D-42 invariant 1 -- known source values. Single query, same shape
        # as the price_source gate above: a data defect at any coverage
        # level, so it runs unconditionally (not gated behind
        # --min-coverage) since this command runs in the Docker build path
        # where the gate needs to fire, and is the only check in this phase
        # runnable against a real database including production.
        opis_source_count = Station.objects.filter(source=StationSource.OPIS).count()
        overture_source_count = Station.objects.filter(
            source=StationSource.OVERTURE
        ).count()
        unrecognized_source_count = Station.objects.exclude(
            source__in=StationSource.values
        ).count()

        self.stdout.write(
            f"Source breakdown: opis={opis_source_count} "
            f"overture={overture_source_count} "
            f"unrecognized={unrecognized_source_count}"
        )

        if unrecognized_source_count:
            raise CommandError(
                f"{unrecognized_source_count} station row(s) hold a source "
                f"value outside {StationSource.values}"
            )

        # D-42 invariant 2 -- Overture rows are well-formed: every
        # Overture-sourced row carries a non-blank gers_id AND an opis_id
        # inside the reserved Overture span. Runs unconditionally, same
        # reasoning as invariant 1.
        overture_missing_gers_id_count = Station.objects.filter(
            source=StationSource.OVERTURE
        ).filter(Q(gers_id__isnull=True) | Q(gers_id__exact="")).count()
        overture_id_out_of_range_count = (
            Station.objects.filter(source=StationSource.OVERTURE)
            .exclude(
                opis_id__gte=OVERTURE_ID_RANGE[0], opis_id__lt=OVERTURE_ID_RANGE[1]
            )
            .count()
        )

        self.stdout.write(
            f"Overture row check: total={overture_source_count} "
            f"missing_gers_id={overture_missing_gers_id_count} "
            f"id_out_of_range={overture_id_out_of_range_count}"
        )

        if overture_missing_gers_id_count:
            raise CommandError(
                f"{overture_missing_gers_id_count} Overture-sourced station "
                "row(s) have a blank gers_id"
            )
        if overture_id_out_of_range_count:
            raise CommandError(
                f"{overture_id_out_of_range_count} Overture-sourced station "
                f"row(s) have an opis_id outside the reserved span "
                f"{OVERTURE_ID_RANGE}"
            )

        # D-42 invariant 3 -- OPIS ids stay outside the reserved span (the
        # disjointness assertion checked from the other direction).
        # max_opis_id is reported alongside the count so the four-orders-
        # of-magnitude headroom (today's real max OPIS id, 73,131, against
        # OVERTURE_ID_RANGE's floor of 1,000,000,000) is visible in the
        # command's own output, not only in a docstring. Runs
        # unconditionally, same reasoning as invariants 1 and 2.
        opis_id_in_reserved_span_count = Station.objects.filter(
            source=StationSource.OPIS,
            opis_id__gte=OVERTURE_ID_RANGE[0],
            opis_id__lt=OVERTURE_ID_RANGE[1],
        ).count()
        max_opis_id = Station.objects.filter(
            source=StationSource.OPIS
        ).aggregate(Max("opis_id"))["opis_id__max"]

        self.stdout.write(
            f"OPIS id-range check: in_reserved_span="
            f"{opis_id_in_reserved_span_count} max_opis_id={max_opis_id}"
        )

        if opis_id_in_reserved_span_count:
            raise CommandError(
                f"{opis_id_in_reserved_span_count} OPIS-sourced station "
                f"row(s) have an opis_id inside the reserved Overture span "
                f"{OVERTURE_ID_RANGE}"
            )

        # D-42 invariant 4 -- the price basis has not drifted (D-11). Not a
        # simple .filter().count(): each estimate-priced row's retail_price
        # must equal its OWN region's baseline, a per-row computed
        # comparison, so this iterates with .values_list() (never full
        # model instances, to keep this cheap over thousands of rows in
        # the Docker build path) and compares in Python, the same pattern
        # this command already uses for its coverage math above.
        #
        # Exact equality, not a tolerance: the committed CSV claims its
        # estimate rows are priced at their region's EIA baseline, and the
        # request-time identity that makes the shown price the current
        # regional average depends on that equality holding exactly. A
        # baseline rebase must fail loudly here rather than letting the
        # committed data silently stop meaning what it claims. Runs
        # unconditionally, same reasoning as invariants 1-3.
        estimate_rows = Station.objects.filter(
            price_source=PriceSource.EIA_REGIONAL_ESTIMATE
        ).values_list("opis_id", "name", "state", "retail_price")

        estimate_checked_count = 0
        estimate_mismatched = []
        for opis_id, name, state, retail_price in estimate_rows:
            estimate_checked_count += 1
            region = regions.region_for_state(state)
            baseline = regions.BASELINE_VALUES.get(region) if region else None
            if baseline is None or retail_price != baseline:
                estimate_mismatched.append(
                    (opis_id, name, state, retail_price, baseline)
                )

        self.stdout.write(
            f"Price-basis check (D-11): checked={estimate_checked_count} "
            f"mismatched={len(estimate_mismatched)}"
        )

        if estimate_mismatched:
            offenders = ", ".join(
                f"{opis_id} ({name}, {state}): {retail_price} != {baseline}"
                for opis_id, name, state, retail_price, baseline in (
                    estimate_mismatched[:5]
                )
            )
            raise CommandError(
                f"{len(estimate_mismatched)} estimate-priced station row(s) "
                f"have a retail_price that does not exactly equal their "
                f"region's EIA baseline: {offenders}"
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
