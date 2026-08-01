"""Report ROADMAP criterion 1's before/after objective evidence: the
frozen greedy baseline re-measured against the committed-CSV-replayed
station table FIRST, then the DP measured on the exact same basis
(D-28) -- one before/after table across the twelve pinned corridors, with
the trivial-stop share and the fuel-cost/savings delta all in one place
(D-29/D-30/D-37).

Read-only: no writes of its own, no network calls. This command DOES
trigger `seed_stations` -- the committed-CSV idempotent replay, not a
mutation invented here -- so every run measures against a reproducible
station table rather than whatever happens to already be in the local
database (the likely cause of the historical scoping-time survey's
figures never reproducing, see the side-by-side comparison this command
prints). Works with no routing-provider token set -- the twelve corridor
geometries are committed fixtures, replayed offline through the existing
Directions-response parser.

Must NOT run in CI -- the figures below are evidence, not a pass/fail
gate, exactly as measure_prune_reduction.py and
measure_penalty_disagreement.py are evidence for their own subjects. The
CI-enforcing guards for the claims this command reports in full are
`PlanObjectiveGuardTests` (D-31's loose Dallas -> Seattle bound) and
`PenaltyNativeReasonCorridorTests` (D-05's pinned real observation), both
in routing/tests/test_plan_objective.py.

Per D-28/D-30/D-31, this module defines no corridor, tank range, price
basis, vehicle, penalty or trivial-stop threshold of its own: every value
printed below is read off OBJECTIVE_PARAMS / TRIVIAL_STOP_TANK_FRACTION /
CORRIDORS, imported from routing.tests.test_plan_objective /
routing.tests.test_corridor_fixtures, the single shared source of truth
pinned before any measurement was taken.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from routing.models import Station
from routing.services import corridor, naive_baseline
from routing.services.exceptions import InfeasibleRouteError
from routing.tests.frozen_greedy import solve as frozen_greedy_solve
from routing.tests.test_corridor_fixtures import (
    CORRIDORS,
    factor_lookup_for_basis,
    load_corridor_route,
)
from routing.tests.test_plan_objective import OBJECTIVE_PARAMS, TRIVIAL_STOP_TANK_FRACTION

# The historical scoping-time figures ROADMAP.md's "evidence base" line
# carries, from a harness that no longer exists in git or on disk
# (D-28) -- reported side by side with this run's measured figures,
# never reconciled.
_HISTORICAL_DALLAS_SEATTLE_STOPS = 12
_HISTORICAL_TOTAL_STOPS = 45
_HISTORICAL_TRIVIAL_STOPS = 26
_HISTORICAL_REMOVAL_COST_USD = Decimal("3.96")
_HISTORICAL_REMOVAL_COST_PERCENT = Decimal("0.0082")


@dataclass
class CorridorObjectiveRow:
    """One measured corridor's greedy and (once task 2 lands) DP figures,
    both at OBJECTIVE_PARAMS. DP fields default to `None` -- unpopulated
    until the DP arm has actually run."""

    slug: str
    label: str
    total_route_mi: Decimal
    raw_candidate_count: int
    greedy_infeasible: bool
    greedy_stops: int | None = None
    greedy_trivial: int | None = None
    greedy_cost: Decimal | None = None
    greedy_savings_amount: Decimal | None = None
    greedy_savings_percent: Decimal | None = None
    dp_infeasible: bool | None = None
    dp_strategy: str | None = None
    dp_stops: int | None = None
    dp_trivial: int | None = None
    dp_cost: Decimal | None = None
    dp_savings_amount: Decimal | None = None
    dp_savings_percent: Decimal | None = None


def _trivial_stop_count(plan, *, tank_range_mi, mpg):
    """Stops whose purchase is under TRIVIAL_STOP_TANK_FRACTION of tank
    capacity, in gallons -- ROADMAP criterion 1's own definition."""
    tank_capacity_gal = tank_range_mi / mpg
    threshold_gal = tank_capacity_gal * TRIVIAL_STOP_TANK_FRACTION
    return sum(1 for s in plan.stops if s.gallons < threshold_gal)


def _station_provenance():
    """Live counts off the currently-seeded Station table -- read-only,
    never hardcoded, so this line can never drift from the table the
    figures below were actually measured against (D-28)."""
    total = Station.objects.count()
    routable = Station.objects.routable().count()
    return total, routable


class Command(BaseCommand):
    help = (
        "Report the greedy baseline and the DP's before/after objective "
        "across the twelve pinned real-geometry corridors, re-measured "
        "against the committed-CSV-replayed station table. Read-only "
        "beyond the seed_stations replay it triggers; no network calls; "
        "works with no routing-provider token set. Must NOT run in CI -- "
        "the figures are evidence, not a pass/fail gate; "
        "PlanObjectiveGuardTests and PenaltyNativeReasonCorridorTests in "
        "routing/tests/test_plan_objective.py are the CI-enforcing guards "
        "that exist instead."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--penalty",
            type=str,
            default=str(OBJECTIVE_PARAMS.penalty),
            help=(
                "Fixed-charge penalty in dollars for the DP arm (default: "
                "OBJECTIVE_PARAMS.penalty, the sourced $35 figure)."
            ),
        )

    def handle(self, *args, **options):
        try:
            penalty = Decimal(options["penalty"])
        except InvalidOperation:
            raise CommandError(
                f"--penalty could not be parsed as a Decimal: {options['penalty']!r}"
            )
        if penalty < 0:
            raise CommandError(f"--penalty must not be negative, got {penalty}")

        self._reseed()

        factor_for = factor_lookup_for_basis(OBJECTIVE_PARAMS.price_basis)
        rows = [self._measure_corridor(c, factor_for, penalty) for c in CORRIDORS]

        self._print_greedy_table(rows)
        self._print_d24_style_disclaimer()

    def _reseed(self):
        self.stdout.write(
            "Rebuilding the station table from the committed CSV "
            "(manage.py seed_stations, idempotent replay, no network "
            "call)..."
        )
        call_command("seed_stations", stdout=self.stdout)
        corridor.reset_index()
        total, routable = _station_provenance()
        self.stdout.write(
            self.style.SUCCESS(
                f"Station table: {total} total row(s), {routable} routable "
                "(geocoded). Every figure below is measured against this "
                "table (D-28)."
            )
        )
        self.stdout.write("")

    def _measure_corridor(self, corridor_def, factor_for, penalty):
        route = load_corridor_route(corridor_def.slug)
        candidates = corridor.candidates(route, factor_for=factor_for)

        row = CorridorObjectiveRow(
            slug=corridor_def.slug,
            label=corridor_def.label,
            total_route_mi=route.total_route_mi,
            raw_candidate_count=len(candidates),
            greedy_infeasible=False,
        )

        try:
            greedy_plan = frozen_greedy_solve(
                candidates,
                route.total_route_mi,
                tank_range_mi=OBJECTIVE_PARAMS.tank_range_mi,
                mpg=OBJECTIVE_PARAMS.mpg,
                starting_fuel=OBJECTIVE_PARAMS.starting_fuel,
            )
        except InfeasibleRouteError:
            row.greedy_infeasible = True
            return row

        naive_plan = naive_baseline.solve(
            candidates,
            route.total_route_mi,
            tank_range_mi=OBJECTIVE_PARAMS.tank_range_mi,
            mpg=OBJECTIVE_PARAMS.mpg,
            starting_fuel=OBJECTIVE_PARAMS.starting_fuel,
        )
        savings = naive_baseline.compute_savings(greedy_plan, naive_plan)

        row.greedy_stops = len(greedy_plan.stops)
        row.greedy_trivial = _trivial_stop_count(
            greedy_plan, tank_range_mi=OBJECTIVE_PARAMS.tank_range_mi, mpg=OBJECTIVE_PARAMS.mpg
        )
        row.greedy_cost = greedy_plan.total_cost
        row.greedy_savings_amount = savings.amount
        row.greedy_savings_percent = savings.percent
        return row

    def _print_greedy_table(self, rows):
        self.stdout.write(
            self.style.SUCCESS(
                "Greedy baseline (routing.tests.frozen_greedy -- the "
                f"pre-Phase-18 referee, NOT solver.solve()) at "
                f"mpg={OBJECTIVE_PARAMS.mpg}, "
                f"tank_range_mi={OBJECTIVE_PARAMS.tank_range_mi}, "
                f"starting_fuel={OBJECTIVE_PARAMS.starting_fuel}, "
                f"price_basis={OBJECTIVE_PARAMS.price_basis!r}:"
            )
        )
        total_stops = 0
        total_trivial = 0
        infeasible = 0
        for row in rows:
            if row.greedy_infeasible:
                infeasible += 1
                self.stdout.write(
                    f"    {row.label} ({row.slug}): INFEASIBLE at this tank range"
                )
                continue
            total_stops += row.greedy_stops
            total_trivial += row.greedy_trivial
            self.stdout.write(
                f"    {row.label} ({row.slug}): route={row.total_route_mi}mi "
                f"raw_candidates={row.raw_candidate_count} "
                f"stops={row.greedy_stops} trivial={row.greedy_trivial} "
                f"cost=${row.greedy_cost:.2f} "
                f"savings=${row.greedy_savings_amount:.2f} "
                f"({self._pct(row.greedy_savings_percent)})"
            )
        self.stdout.write("")
        self.stdout.write(
            f"Aggregate (greedy): total_stops={total_stops} "
            f"total_trivial={total_trivial} infeasible={infeasible}/12"
        )

    @staticmethod
    def _pct(value):
        if value is None:
            return "n/a"
        return f"{value * 100:.2f}%"

    def _print_d24_style_disclaimer(self):
        self.stdout.write("")
        self.stdout.write(
            "This report measures the greedy baseline's own objective "
            "figures only -- the DP arm and the combined before/after "
            "table land in plan 18-05's task 2. It makes NO latency claim "
            "(Phase 18 criterion 5's measured solver latency is proven "
            "elsewhere) and measures exactly one vehicle profile, one "
            "penalty, and one price basis."
        )
