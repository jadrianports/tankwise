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

This command makes NO latency claim -- Phase 18 criterion 5's measured
solver latency is proven elsewhere (18-04c-SUMMARY.md's calibration
table, and plan 18-06/18-07's own work). It reports exactly one vehicle
profile and one penalty as the headline; the `eia_fixture` price basis is
reported alongside the neutral headline, never merged into it (D-30/D-37).
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from routing.models import Station
from routing.services import corridor, naive_baseline, solver
from routing.services.exceptions import InfeasibleRouteError
from routing.tests.frozen_greedy import solve as frozen_greedy_solve
from routing.tests.test_corridor_fixtures import (
    CORRIDORS,
    PRICE_BASIS_EIA,
    factor_lookup_for_basis,
    load_corridor_route,
)
from routing.tests.test_plan_objective import OBJECTIVE_PARAMS, TRIVIAL_STOP_TANK_FRACTION

# The historical scoping-time figures ROADMAP.md's "evidence base" line
# carries, from a harness that no longer exists in git or on disk
# (D-28) -- reported side by side with this run's measured figures,
# never reconciled. No parameter above was adjusted after seeing how
# this run's own figures compare to these.
_HISTORICAL_DALLAS_SEATTLE_STOPS = 12
_HISTORICAL_TOTAL_STOPS = 45
_HISTORICAL_TRIVIAL_STOPS = 26
_HISTORICAL_REMOVAL_COST_USD = Decimal("3.96")
_HISTORICAL_REMOVAL_COST_PERCENT = Decimal("0.0082")


@dataclass
class CorridorObjectiveRow:
    """One measured corridor's greedy and DP figures, both at
    OBJECTIVE_PARAMS's neutral basis. DP fields default to `None` --
    unpopulated for a corridor whose greedy arm was infeasible (the DP
    arm is skipped in that case, since a route infeasible for one
    algorithm's candidate set is infeasible for the other -- both read
    the same preflight_gap_check)."""

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

        neutral_factor_for = factor_lookup_for_basis(OBJECTIVE_PARAMS.price_basis)
        rows = [self._measure_corridor(c, neutral_factor_for, penalty) for c in CORRIDORS]

        self._print_before_after_table(rows)
        self._print_aggregates(rows)
        self._print_savings_delta(rows)
        self._print_eia_alongside(penalty)
        self._print_historical_comparison(rows)
        self._print_disclaimer()

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
        greedy_savings = naive_baseline.compute_savings(greedy_plan, naive_plan)

        row.greedy_stops = len(greedy_plan.stops)
        row.greedy_trivial = _trivial_stop_count(
            greedy_plan, tank_range_mi=OBJECTIVE_PARAMS.tank_range_mi, mpg=OBJECTIVE_PARAMS.mpg
        )
        row.greedy_cost = greedy_plan.total_cost
        row.greedy_savings_amount = greedy_savings.amount
        row.greedy_savings_percent = greedy_savings.percent

        # The DP arm -- the real production seam (solver.solve()), never
        # dp.solve_fixed_charge called directly, so this measures exactly
        # what a live request would return, strategy dispatch included.
        try:
            dp_plan = solver.solve(
                candidates,
                route.total_route_mi,
                tank_range_mi=OBJECTIVE_PARAMS.tank_range_mi,
                mpg=OBJECTIVE_PARAMS.mpg,
                starting_fuel=OBJECTIVE_PARAMS.starting_fuel,
                penalty=penalty,
            )
        except InfeasibleRouteError:
            row.dp_infeasible = True
            return row

        dp_savings = naive_baseline.compute_savings(dp_plan, naive_plan)

        row.dp_infeasible = False
        row.dp_strategy = dp_plan.strategy
        row.dp_stops = len(dp_plan.stops)
        row.dp_trivial = _trivial_stop_count(
            dp_plan, tank_range_mi=OBJECTIVE_PARAMS.tank_range_mi, mpg=OBJECTIVE_PARAMS.mpg
        )
        row.dp_cost = dp_plan.total_cost
        row.dp_savings_amount = dp_savings.amount
        row.dp_savings_percent = dp_savings.percent
        return row

    def _print_before_after_table(self, rows):
        self.stdout.write(
            self.style.SUCCESS(
                "Before/after objective, neutral basis (headline) -- "
                f"greedy = routing.tests.frozen_greedy (pre-Phase-18 "
                f"referee); DP = routing.services.solver.solve() (the "
                f"live production seam, dispatches exact_dp or "
                f"penalty_aware_heuristic per corridor) -- at "
                f"mpg={OBJECTIVE_PARAMS.mpg}, "
                f"tank_range_mi={OBJECTIVE_PARAMS.tank_range_mi}, "
                f"starting_fuel={OBJECTIVE_PARAMS.starting_fuel}, "
                f"penalty=${OBJECTIVE_PARAMS.penalty}:"
            )
        )
        for row in rows:
            if row.greedy_infeasible:
                self.stdout.write(
                    f"    {row.label} ({row.slug}): GREEDY INFEASIBLE at "
                    "this tank range -- DP arm skipped"
                )
                continue
            if row.dp_infeasible:
                self.stdout.write(
                    f"    {row.label} ({row.slug}): greedy stops="
                    f"{row.greedy_stops} trivial={row.greedy_trivial} -> "
                    "DP INFEASIBLE (regression against the greedy's own "
                    "feasibility)"
                )
                continue
            cost_delta = row.dp_cost - row.greedy_cost
            cost_delta_pct = (
                cost_delta / row.greedy_cost * 100 if row.greedy_cost else Decimal(0)
            )
            self.stdout.write(
                f"    {row.label} ({row.slug}) [{row.dp_strategy}]: "
                f"stops {row.greedy_stops}->{row.dp_stops}  "
                f"trivial {row.greedy_trivial}->{row.dp_trivial}  "
                f"cost ${row.greedy_cost:.2f}->${row.dp_cost:.2f} "
                f"(delta ${cost_delta:+.2f}, {cost_delta_pct:+.2f}%)  "
                f"savings ${row.greedy_savings_amount:.2f} "
                f"({self._pct(row.greedy_savings_percent)}) -> "
                f"${row.dp_savings_amount:.2f} "
                f"({self._pct(row.dp_savings_percent)})"
            )

    def _print_aggregates(self, rows):
        measured = [r for r in rows if not r.greedy_infeasible and not r.dp_infeasible]
        greedy_infeasible = sum(1 for r in rows if r.greedy_infeasible)
        dp_infeasible = sum(1 for r in rows if r.dp_infeasible)

        total_greedy_stops = sum(r.greedy_stops for r in measured)
        total_dp_stops = sum(r.dp_stops for r in measured)
        total_greedy_trivial = sum(r.greedy_trivial for r in measured)
        total_dp_trivial = sum(r.dp_trivial for r in measured)

        self.stdout.write("")
        self.stdout.write(
            f"Aggregate across {len(measured)}/12 corridors solved by both arms:"
        )
        self.stdout.write(
            f"    total_stops: greedy={total_greedy_stops} -> dp={total_dp_stops}"
        )
        self.stdout.write(
            f"    total_trivial_stops: greedy={total_greedy_trivial} -> "
            f"dp={total_dp_trivial}"
        )
        self.stdout.write(
            f"    infeasible corridors: greedy={greedy_infeasible}/12 "
            f"dp={dp_infeasible}/12"
        )

    def _print_savings_delta(self, rows):
        self.stdout.write("")
        self.stdout.write(
            "Per-corridor savings delta (named copy material for Phase "
            "19's HON-03 -- the honest framing is a product statement: "
            "TankWise now spends a few dollars more on fuel to remove "
            "several stops from the driver's day):"
        )
        for row in rows:
            if row.greedy_infeasible or row.dp_infeasible:
                continue
            stop_delta = row.dp_stops - row.greedy_stops
            cost_delta = row.dp_cost - row.greedy_cost
            self.stdout.write(
                f"    {row.label}: {stop_delta:+d} stops, "
                f"${cost_delta:+.2f} fuel cost, savings "
                f"${row.greedy_savings_amount:.2f} -> "
                f"${row.dp_savings_amount:.2f}"
            )

    def _print_eia_alongside(self, penalty):
        """The eia_fixture price basis, reported alongside the neutral
        headline above -- never merged into it (D-30/D-37). Domination
        and the DP's objective are both price-ORDER-sensitive, so this
        hands Phase 18 a second, independently-computed data point on the
        EIA-x-penalty interaction STATE.md flags as uncharacterised,
        without changing which basis carries the headline claim."""
        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Alongside, eia_fixture basis (NOT the headline, NOT "
                "merged into the table above) -- DP arm only, same "
                "vehicle/penalty:"
            )
        )
        eia_factor_for = factor_lookup_for_basis(PRICE_BASIS_EIA)
        total_stops = 0
        total_cost = Decimal(0)
        infeasible = 0
        for corridor_def in CORRIDORS:
            route = load_corridor_route(corridor_def.slug)
            candidates = corridor.candidates(route, factor_for=eia_factor_for)
            try:
                dp_plan = solver.solve(
                    candidates,
                    route.total_route_mi,
                    tank_range_mi=OBJECTIVE_PARAMS.tank_range_mi,
                    mpg=OBJECTIVE_PARAMS.mpg,
                    starting_fuel=OBJECTIVE_PARAMS.starting_fuel,
                    penalty=penalty,
                )
            except InfeasibleRouteError:
                infeasible += 1
                self.stdout.write(f"    {corridor_def.label}: INFEASIBLE")
                continue
            total_stops += len(dp_plan.stops)
            total_cost += dp_plan.total_cost
            self.stdout.write(
                f"    {corridor_def.label} [{dp_plan.strategy}]: "
                f"stops={len(dp_plan.stops)} cost=${dp_plan.total_cost:.2f}"
            )
        self.stdout.write(
            f"    eia_fixture aggregate: total_stops={total_stops} "
            f"total_cost=${total_cost:.2f} infeasible={infeasible}/12"
        )

    @staticmethod
    def _pct(value):
        if value is None:
            return "n/a"
        return f"{value * 100:.2f}%"

    def _print_historical_comparison(self, rows):
        """D-28/D-37: the historical scoping-time figures and this run's
        measured figures, side by side, explicitly NOT reconciled. No
        parameter above was tuned after seeing how they compare."""
        dallas_row = next((r for r in rows if r.slug == "dallas_tx-seattle_wa"), None)
        measured = [r for r in rows if not r.greedy_infeasible]
        measured_total_stops = sum(r.greedy_stops for r in measured)
        measured_total_trivial = sum(r.greedy_trivial for r in measured)

        self.stdout.write("")
        self.stdout.write(
            self.style.WARNING(
                "Historical vs. measured (D-28) -- reported side by side "
                "and deliberately NOT reconciled; neither validates the "
                "other. No parameter (corridor, tank range, price basis, "
                "vehicle profile, or penalty) was adjusted after seeing "
                "this run's own greedy figures above."
            )
        )
        if dallas_row is not None and not dallas_row.greedy_infeasible:
            self.stdout.write(
                f"    Dallas->Seattle greedy stops: historical="
                f"{_HISTORICAL_DALLAS_SEATTLE_STOPS} measured="
                f"{dallas_row.greedy_stops}"
            )
        self.stdout.write(
            f"    Total greedy stops (12 corridors): historical="
            f"{_HISTORICAL_TOTAL_STOPS} measured={measured_total_stops}"
        )
        self.stdout.write(
            f"    Total trivial greedy stops: historical="
            f"{_HISTORICAL_TRIVIAL_STOPS} measured={measured_total_trivial}"
        )
        self.stdout.write(
            f"    Historical removal cost: +${_HISTORICAL_REMOVAL_COST_USD} "
            f"({_HISTORICAL_REMOVAL_COST_PERCENT * 100:.2f}%) -- this "
            "run's own measured removal cost is the per-corridor delta "
            "table printed above."
        )
        if measured_total_stops != _HISTORICAL_TOTAL_STOPS or (
            dallas_row is not None
            and not dallas_row.greedy_infeasible
            and dallas_row.greedy_stops != _HISTORICAL_DALLAS_SEATTLE_STOPS
        ):
            self.stdout.write(
                "    These figures differ from the historical scoping-time "
                "survey. The likely cause (not confirmed, per D-28's "
                "instruction not to claim certainty) is that the "
                "scoping-time survey ran against a local db.sqlite3 that "
                "had drifted from the committed CSV, rather than the "
                "Phase 17 D-34 idempotent replay this command performs."
            )

    def _print_disclaimer(self):
        self.stdout.write("")
        self.stdout.write(
            "This report measures the objective (stop count, trivial "
            "share, fuel cost, savings) only and makes NO latency claim -- "
            "Phase 18 criterion 5's measured solver latency is "
            "18-04c-SUMMARY.md's and plan 18-06/18-07's subject. It "
            "measures exactly one vehicle profile and one penalty as the "
            "headline; the eia_fixture basis above is reported alongside, "
            "never merged into the neutral headline. "
            "PlanObjectiveGuardTests and PenaltyNativeReasonCorridorTests "
            "are the CI-enforcing gates that protect the claims reported "
            "here in full."
        )
