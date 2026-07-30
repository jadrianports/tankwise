"""Measure how often the shipped price-blind greedy is beaten by the
penalty-aware fixed-charge oracle on a fixed, seeded long-haul corpus.

Read-only: no writes, no database access, no network calls. The corpus is
synthesized entirely offline by routing.tests.test_solver_fixed_charge_optimality's
build_corpus(); Mapbox and the Station table are never touched.

Must NOT run in CI -- mirroring benchmark_corridor.py's own disclaimer, the
figure this command prints is evidence, not a pass/fail gate. The thing
that IS the CI gate is PenaltyDisagreementFloorTests in
routing/tests/test_solver_fixed_charge_optimality.py, which asserts only a
loose floor on a small slice of the same corpus, on every commit. This
command exists to report the precise headline number that floor merely
guards.

Per D-16, this module defines no corpus constant of its own: every corpus
value printed below is read off CORPUS_PARAMS, imported from the test
module, which is the single shared source of truth.
"""
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError

from routing.tests.test_solver_fixed_charge_optimality import (
    CORPUS_PARAMS,
    measure_disagreement,
)


class Command(BaseCommand):
    help = (
        "Measure how often the shipped greedy's penalised objective is beaten "
        "by the fixed-charge oracle's true optimum, on a fixed, seeded "
        "long-haul corpus. Read-only: no writes, no network calls. Must NOT "
        "run in CI -- the figure is evidence, not a pass/fail gate; "
        "PenaltyDisagreementFloorTests is the CI-enforcing guard."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--routes",
            type=int,
            default=200,
            help=(
                "Number of corpus routes to measure over (default 200 -- "
                "large enough for a stable figure, and a superset of the "
                "in-suite guard's routes because the corpus RNG is consumed "
                "sequentially)."
            ),
        )
        parser.add_argument(
            "--penalty",
            type=str,
            default=str(CORPUS_PARAMS.penalty),
            help=(
                "Fixed-charge penalty in dollars to measure at (default "
                "CORPUS_PARAMS.penalty, the sourced $35 figure). Exposed so "
                "Phase 18 can re-measure against the real DP at other "
                "penalties."
            ),
        )

    def handle(self, *args, **options):
        n_routes = options["routes"]
        if n_routes < 1:
            raise CommandError(f"--routes must be >= 1, got {n_routes}")

        try:
            penalty = Decimal(options["penalty"])
        except InvalidOperation:
            raise CommandError(f"--penalty could not be parsed as a Decimal: {options['penalty']!r}")
        if penalty < 0:
            raise CommandError(f"--penalty must not be negative, got {penalty}")

        report = measure_disagreement(n_routes, penalty=penalty)
        params = report.params
        rate_pct = report.rate * 100

        self.stdout.write(
            self.style.SUCCESS(
                f"Disagreement rate at penalty=${penalty}: {rate_pct:.2f}% "
                f"({report.n_disagree}/{report.n_compared} routes)"
            )
        )
        self.stdout.write(
            f"Corpus: seed={params.seed}, routes requested={n_routes}, "
            f"routes compared={report.n_compared}, "
            f"infeasible (excluded)={report.n_infeasible}"
        )
        self.stdout.write(
            f"Vehicle: mpg={params.mpg}, tank_range_mi={params.tank_range_mi}, "
            f"starting_fuel={params.starting_fuel}"
        )
        self.stdout.write(
            f"Route length band: [{params.min_route_mi}, {params.max_route_mi}] mi"
        )
        self.stdout.write(f"Stations per route: {params.stations_per_route}")
        self.stdout.write(
            f"Price band: [${params.min_price}, ${params.max_price}] per gallon"
        )

        self.stdout.write("")
        self.stdout.write(
            "This figure was measured on the corpus and seed printed above. A "
            "separate, scoping-time measurement (taken before this phase, on a "
            "harness that no longer exists -- nothing in git, nothing on disk) "
            "found the greedy beaten in 157 of 286 randomized trials (55%). "
            "The two figures are reported side by side and are deliberately "
            "not reconciled -- neither should be read as validating the other."
        )
