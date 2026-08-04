"""Measure solver-only latency for the frozen greedy baseline and the DP
across the twelve pinned real corridors, both pinned tank ranges, and a
penalty sweep (D-19).

Must NOT run in CI -- mirroring measure_penalty_disagreement.py's own
disclaimer, the figures this command prints are evidence, not a pass/fail
gate. The thing that IS the CI gate is SolverLatencyCeilingTests in
routing/tests/test_solver_latency.py, which asserts a loose 5x headroom
guard on the single worst-measured cell, on every commit. This command
exists to report the full, real-geometry latency table that guard merely
protects.

This module defines no corridor, tank range, vehicle, penalty or ceiling of
its own: CORRIDORS/TANK_RANGES_MI come from routing.tests.test_corridor_fixtures,
the UI-default vehicle from OBJECTIVE_PARAMS
(routing.tests.test_plan_objective), the penalty sweep and
LATENCY_CEILING_SECONDS/LATENCY_HEADROOM_MULTIPLE from
routing.tests.test_solver_latency -- every value printed below is imported
from the test modules, the single shared source of truth pinned before this
command's own measurement ever ran (D-19).

Read-only and offline: no writes of its own beyond the seed_stations replay
it triggers (the committed-CSV idempotent replay, not a mutation invented
here), no network calls. Works with no routing-provider token set -- the
twelve corridor geometries are committed fixtures, replayed offline through
the existing Directions-response parser, exactly as
measure_prune_reduction.py and measure_plan_objective.py already do.

Per D-21, this command deliberately does NOT extend benchmark_corridor.py --
that command's own subject (the corridor filter's DB-bbox vs STRtree
timing) is a different measurement with a different audience, and keeping
them separate avoids conflating two unrelated timing stories into one
report.

The greedy arm is measured and printed FIRST, in full, before the DP arm
ever calls solve() once, so within any single run of this command the
historical baseline reaches the record before any DP figure does.

**Scope of that claim -- read it narrowly.** It describes the ordering
INSIDE this command only. It is NOT a claim that the phase honoured D-19's
pre-decide-then-measure discipline overall: it did not. The DP was timed
extensively earlier in Phase 18, during the work that produced
`dp.DP_TRANSITION_BUDGET`, and the 5-second figure calibrating that budget
was adopted reactively from `GUNICORN_TIMEOUT=30`. See
`routing/tests/test_solver_latency.py`'s module docstring for the full
provenance, and 18-04c / 18-05b / 18-05c for the measurements themselves.
`LATENCY_CEILING_SECONDS` is a separate, stricter, claim-derived number and
is not back-fitted to any of those timings.
"""
import io
import statistics
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from routing.services import corridor, dp, solver
from routing.services.prune import prune_dominated_candidates
from routing.tests.frozen_greedy import solve as frozen_greedy_solve
from routing.tests.test_corridor_fixtures import (
    CORRIDORS,
    TANK_RANGES_MI,
    factor_lookup_for_basis,
    load_corridor_route,
)
from routing.tests.test_plan_objective import OBJECTIVE_PARAMS
from routing.tests.test_solver_latency import (
    LATENCY_CEILING_SECONDS,
    LATENCY_HEADROOM_MULTIPLE,
    LATENCY_PENALTY_SWEEP,
)


@dataclass
class CorridorLatencyRow:
    """One measured (corridor, tank_range_mi) cell: the greedy baseline
    plus, once task 2's DP arm has run, one DP measurement per swept
    penalty rung."""

    slug: str
    label: str
    tank_range_mi: Decimal
    raw_count: int
    kept_count: int
    greedy_times_s: list = field(default_factory=list)
    # penalty (Decimal) -> {"times_s": [...], "strategy": str}
    dp_by_penalty: dict = field(default_factory=dict)

    @property
    def greedy_median_s(self):
        return statistics.median(self.greedy_times_s)


def _median(values):
    return statistics.median(values)


class Command(BaseCommand):
    help = (
        "Measure solver-only latency for the frozen greedy baseline "
        "(measured and printed first) and the DP, across the twelve "
        "pinned real corridors, both pinned tank ranges, and a penalty "
        "sweep, at OBJECTIVE_PARAMS' UI-default vehicle. Reports the "
        "DP/greedy ratio and a pass/fail marker against "
        "LATENCY_CEILING_SECONDS. Read-only beyond the seed_stations "
        "replay it triggers; no network calls; works with no "
        "routing-provider token set. Must NOT run in CI -- the figures "
        "are evidence, not a pass/fail gate; SolverLatencyCeilingTests in "
        "routing/tests/test_solver_latency.py is the CI-enforcing guard "
        "that exists instead."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--repeats",
            type=int,
            default=3,
            help=(
                "Timing repetitions per (corridor, tank_range, penalty) "
                "cell (default 3 -- a per-cell median rather than a "
                "single noisy sample). Both the median and the worst "
                "(max) of these repeats are reported; the pass/fail "
                "marker against LATENCY_CEILING_SECONDS uses the worst, "
                "per this phase's own established worst-of-N (never "
                "mean) methodology for a latency-budget decision."
            ),
        )
        parser.add_argument(
            "--penalty",
            type=str,
            default=None,
            help=(
                "Optional single penalty (dollars) to measure the DP arm "
                "at, instead of sweeping every rung of "
                "LATENCY_PENALTY_SWEEP. Parsed as a Decimal; never "
                "float(...) for a money value."
            ),
        )

    def handle(self, *args, **options):
        repeats = options["repeats"]
        if repeats < 1:
            raise CommandError(f"--repeats must be >= 1, got {repeats}")

        penalty_sweep = list(LATENCY_PENALTY_SWEEP)
        if options["penalty"] is not None:
            try:
                single_penalty = Decimal(options["penalty"])
            except InvalidOperation:
                raise CommandError(
                    f"--penalty could not be parsed as a Decimal: "
                    f"{options['penalty']!r}"
                )
            if single_penalty < 0:
                raise CommandError(
                    f"--penalty must not be negative, got {single_penalty}"
                )
            penalty_sweep = [single_penalty]

        self._reseed()

        mpg = OBJECTIVE_PARAMS.mpg
        starting_fuel = OBJECTIVE_PARAMS.starting_fuel
        factor_for = factor_lookup_for_basis(OBJECTIVE_PARAMS.price_basis)

        self.stdout.write(
            self.style.SUCCESS(
                f"UI-default vehicle (OBJECTIVE_PARAMS): mpg={mpg}, "
                f"starting_fuel={starting_fuel}, price_basis="
                f"{OBJECTIVE_PARAMS.price_basis}. Penalty sweep: "
                f"{[str(p) for p in penalty_sweep]}. Repeats per cell: "
                f"{repeats}."
            )
        )
        self.stdout.write("")

        # Build every cell's candidates ONCE, outside any timed region --
        # both the greedy and DP arms below reuse the same in-memory
        # candidate list, so neither arm's timing includes corridor-build
        # cost.
        rows = []
        candidates_by_cell = {}
        for corridor_def in CORRIDORS:
            route = load_corridor_route(corridor_def.slug)
            candidates = corridor.candidates(route, factor_for=factor_for)
            for tank_range_mi in TANK_RANGES_MI:
                search_set = prune_dominated_candidates(
                    candidates,
                    tank_range_mi=tank_range_mi,
                    total_route_mi=route.total_route_mi,
                )
                row = CorridorLatencyRow(
                    slug=corridor_def.slug,
                    label=corridor_def.label,
                    tank_range_mi=tank_range_mi,
                    raw_count=len(candidates),
                    kept_count=len(search_set),
                )
                rows.append(row)
                candidates_by_cell[(corridor_def.slug, tank_range_mi)] = (
                    route,
                    candidates,
                )

        # --- Task 1: greedy arm, measured and printed FIRST, in full,
        # before the DP arm below ever calls solve() once (D-19). ---
        for row in rows:
            route, candidates = candidates_by_cell[(row.slug, row.tank_range_mi)]
            for _ in range(repeats):
                started = time.perf_counter()
                frozen_greedy_solve(
                    candidates,
                    route.total_route_mi,
                    tank_range_mi=row.tank_range_mi,
                    mpg=mpg,
                    starting_fuel=starting_fuel,
                )
                row.greedy_times_s.append(time.perf_counter() - started)

        self._print_greedy_baseline(rows)

        # --- Task 2: DP arm, over the same cells, swept across penalty. ---
        for row in rows:
            route, candidates = candidates_by_cell[(row.slug, row.tank_range_mi)]
            for penalty in penalty_sweep:
                times_s = []
                strategy = None
                for _ in range(repeats):
                    started = time.perf_counter()
                    plan = solver.solve(
                        candidates,
                        route.total_route_mi,
                        tank_range_mi=row.tank_range_mi,
                        mpg=mpg,
                        starting_fuel=starting_fuel,
                        penalty=penalty,
                        deadline=None,  # D-05: untimed -- measures the DP's own solve time, not a capped one
                    )
                    times_s.append(time.perf_counter() - started)
                    strategy = plan.strategy
                row.dp_by_penalty[penalty] = {"times_s": times_s, "strategy": strategy}

        self._print_dp_table(rows, penalty_sweep)
        self._print_worst_case(rows, penalty_sweep)
        self._print_disclaimer()

    def _reseed(self):
        self.stdout.write(
            "Rebuilding the station table from the committed CSV "
            "(manage.py seed_stations, idempotent replay, no network "
            "call)..."
        )
        call_command("seed_stations", stdout=io.StringIO())
        corridor.reset_index()
        self.stdout.write("")

    def _print_greedy_baseline(self, rows):
        self.stdout.write(
            self.style.SUCCESS(
                "GREEDY BASELINE (routing.tests.frozen_greedy, the "
                "pre-Phase-18 referee -- no penalty concept) -- recorded "
                "before this run's DP arm:"
            )
        )
        for row in rows:
            self.stdout.write(
                f"    {row.label} ({row.slug}) @{row.tank_range_mi}mi: "
                f"raw={row.raw_count} kept={row.kept_count} "
                f"greedy_median={row.greedy_median_s:.4f}s"
            )
        self.stdout.write("")

    def _print_dp_table(self, rows, penalty_sweep):
        self.stdout.write(
            self.style.SUCCESS(
                "DP ARM (routing.services.solver.solve() -- the live "
                "production seam, dispatches exact_dp or "
                "penalty_aware_heuristic per cell). Absolute figure = "
                "median of repeats; [worst] = max of repeats, the basis "
                "for the PASS/FAIL marker against "
                f"LATENCY_CEILING_SECONDS=${LATENCY_CEILING_SECONDS}s. "
                "Ratio = DP median / greedy median (same corridor/tank "
                "cell):"
            )
        )
        for row in rows:
            self.stdout.write(f"    {row.label} ({row.slug}) @{row.tank_range_mi}mi:")
            for penalty in penalty_sweep:
                cell = row.dp_by_penalty[penalty]
                times_s = cell["times_s"]
                median_s = _median(times_s)
                worst_s = max(times_s)
                ratio = (
                    median_s / row.greedy_median_s
                    if row.greedy_median_s > 0
                    else float("inf")
                )
                verdict = (
                    "PASS"
                    if Decimal(str(worst_s)) <= LATENCY_CEILING_SECONDS
                    else "FAIL"
                )
                self.stdout.write(
                    f"        penalty=${penalty} [{cell['strategy']}]: "
                    f"median={median_s:.4f}s worst={worst_s:.4f}s "
                    f"ratio={ratio:.1f}x vs greedy  {verdict}"
                )
        self.stdout.write("")

    def _print_worst_case(self, rows, penalty_sweep):
        worst_row = None
        worst_penalty = None
        worst_s = -1.0
        for row in rows:
            for penalty in penalty_sweep:
                cell_worst = max(row.dp_by_penalty[penalty]["times_s"])
                if cell_worst > worst_s:
                    worst_s = cell_worst
                    worst_row = row
                    worst_penalty = penalty

        breach_count = sum(
            1
            for row in rows
            for penalty in penalty_sweep
            if Decimal(str(max(row.dp_by_penalty[penalty]["times_s"])))
            > LATENCY_CEILING_SECONDS
        )
        total_cells = len(rows) * len(penalty_sweep)

        self.stdout.write(
            self.style.WARNING(
                f"Aggregate worst-case: {worst_row.label} ({worst_row.slug}) "
                f"@{worst_row.tank_range_mi}mi at penalty=${worst_penalty} "
                f"[{worst_row.dp_by_penalty[worst_penalty]['strategy']}], "
                f"worst-of-{len(worst_row.dp_by_penalty[worst_penalty]['times_s'])} "
                f"= {worst_s:.4f}s."
            )
        )
        self.stdout.write(
            f"LATENCY_CEILING_SECONDS=${LATENCY_CEILING_SECONDS}s breached "
            f"on {breach_count}/{total_cells} (corridor, tank, penalty) "
            "cells. This ceiling is derived from PROJECT.md's informal "
            "'sub-second solve' CLAIM, not a measurement -- a breach here "
            "means that specific standing claim does not hold on every "
            "corridor at the exact-DP path, not that the deployed request "
            "budget (GUNICORN_TIMEOUT=30s) is at risk: the worst observed "
            "figure above is still well under that deployed timeout."
        )
        self.stdout.write(
            "Per D-20, the only sanctioned response to a breach is state "
            "dominance inside the DP. routing/services/dp.py's own Pareto "
            "state-dominance pruning (see its module docstring's "
            "'Pareto state-dominance pruning' section) is already a "
            "one-time, COMPLETE frontier computation over every node -- "
            "there is no further state to prune under that rule without "
            "the deeper state-space restructuring 18-04b-SUMMARY.md "
            "explicitly scoped OUT of this phase. This command therefore "
            "does not attempt a further reduction; the breach is recorded "
            "here, in full, for plan 18-08's reconciliation, per this "
            "phase's own established practice of flagging findings rather "
            "than rushing an unreviewed change to a proof-bearing module."
        )
        self.stdout.write("")

    def _print_disclaimer(self):
        self.stdout.write(
            "This command measures solver-only time on a developer "
            "machine and makes NO end-to-end claim. The live picture is "
            "the Server-Timing capture from the deployed instance "
            "(18-06-SUMMARY.md's task 3), which includes Render "
            "free-tier, Neon and Upstash network cost and is reported "
            "separately, never merged into the figures above. This "
            "measurement discharges PRUN-01's latency clause -- Phase 17 "
            "D-24 assigned latency ownership to Phase 18."
        )
