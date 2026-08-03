"""Measure every candidate predictor (`CANDIDATE_PREDICTORS`, including the
incumbent `dp.estimate_transition_count`) against measured DP wall-clock
time on `PREDICTOR_CELLS`, and apply the pinned verdict from
`routing.tests.test_dispatch_predictor` exactly as written, with no
reinterpretation.

Must NOT run in CI -- mirrors `measure_solver_latency.py`'s own
disclaimer: the table and verdict this command prints are evidence, not a
pass/fail gate. `DispatchPredictorGuardTests`
(`routing.tests.test_dispatch_predictor`, added in a later commit) is the
CI-enforcing half of this (report-only command, in-suite guard) pair.

This module defines no corridor, tank range, vehicle, penalty, budget,
floor or verdict threshold of its own. Every value is imported from
`routing.tests.test_dispatch_predictor` (the seven pinned constants:
`PREDICTOR_CELLS`, `PREDICTOR_CELL_BUDGET_SECONDS`,
`CENSORED_RANK_TREATMENT`, `CANDIDATE_PREDICTORS`, `PREDICTOR_RANK_FLOOR`,
`PREDICTOR_INVERSION_BUDGET`, `DISPATCH_RETENTION_FLOOR`),
`routing.tests.test_corridor_fixtures` (corridor geometry replay, price
basis) and `routing.tests.test_solver_dispatch` (the API-default vehicle
this measurement uses -- `MPG=10`, `STARTING_FUEL=0.5`, `PENALTY=35`, the
exact vehicle `dp.DP_TRANSITION_BUDGET`'s own calibration table was
measured against) -- the single shared sources of truth pinned before this
command's own measurement ever ran.

Read-only and offline: replays the twelve committed corridor geometry
fixtures (`routing/tests/fixtures/corridor_geometry/`) through the
existing Directions-response parser, exactly as `measure_solver_latency.py`
and `measure_prune_reduction.py` already do. No network call; works with
no routing-provider token set. The one write this command triggers is the
same idempotent `seed_stations` CSV replay every other measurement command
in this codebase already performs, never a mutation invented here.

**Why a subprocess, not an in-process timer, for the raw DP call.**
`dp.solve_fixed_charge`'s worst historically-measured cell
(`toronto_oh-hillsboro_or`) has taken 31-43 raw seconds (see
`dp.py`'s own `DP_TRANSITION_BUDGET` calibration comment and
`18-04c-SUMMARY.md`); a later precision fix (`224b0ee`) made
near-boundary `exact_dp` cells measurably SLOWER (`18-05b-SUMMARY.md`), so
this session's own figures could be worse than that historical record. A
Python `Thread` cannot be preemptively killed -- an abandoned thread would
keep contending for the GIL in the background even after this command
"gives up" on it, silently slowing down (and thereby corrupting) every
measurement taken afterward. Each cell's raw DP call therefore runs in its
own spawned child process (`multiprocessing.get_context("spawn")`, the
only start method available on Windows and the portable choice on any
platform), joined with a hard `PREDICTOR_CELL_BUDGET_SECONDS` timeout; a
cell still running past that deadline is `terminate()`-d -- a real
OS-level kill, not a cooperative one -- and recorded censored at the
budget value, per `CENSORED_RANK_TREATMENT`.

This module's own top-level imports are deliberately restricted to stdlib
plus the Django-free `routing.services.dp` (see `SOLVER_FILES` in
`routing/tests/test_boundaries.py` -- `dp.py` carries no Django/ORM/HTTP
import) so a spawned child process can resolve and run the worker
function below without ever needing `django.setup()`. Every
Django-app-dependent import this command itself needs (`corridor`,
`call_command`, every pinned test-module constant) is therefore deferred
to inside `Command.handle()`'s own body, never placed at this module's
top level -- if it were, merely IMPORTING this module (which is exactly
what a spawned child process must do to find the worker function by its
qualified name) would itself try to touch Django's app registry before
`django.setup()` has run in that fresh child interpreter, and fail.
"""
import statistics
import time
from dataclasses import dataclass, field
from decimal import Decimal
from multiprocessing import get_context

from django.core.management.base import BaseCommand

from routing.services import dp


def _solve_fixed_charge_worker(
    queue, candidates, total_route_mi, tank_range_mi, mpg, starting_fuel, penalty
):
    """Runs inside a spawned child process. Deliberately imports nothing
    beyond stdlib plus `routing.services.dp` (Django-free -- see this
    module's own docstring) so the child never needs `django.setup()`.
    Puts `("ok", elapsed_seconds)` or `("error", repr(exc))` on `queue`;
    never lets an exception escape uncommunicated."""
    try:
        started = time.perf_counter()
        dp.solve_fixed_charge(
            candidates,
            total_route_mi=total_route_mi,
            tank_range_mi=tank_range_mi,
            mpg=mpg,
            starting_fuel=starting_fuel,
            penalty=penalty,
        )
        queue.put(("ok", time.perf_counter() - started))
    except Exception as exc:  # noqa: BLE001 - reported to the parent, never swallowed
        queue.put(("error", repr(exc)))


def _time_with_budget(
    budget_seconds, candidates, total_route_mi, tank_range_mi, mpg, starting_fuel, penalty
):
    """Run `dp.solve_fixed_charge` in a fresh child process, hard-killed
    at `budget_seconds` if it has not returned by then.

    Returns `(elapsed_seconds, censored)`: `elapsed_seconds` is the real
    measured time when the child finished inside budget, and exactly
    `budget_seconds` when censored -- matching `CENSORED_RANK_TREATMENT`'s
    own recorded-time convention (`routing.tests.test_dispatch_predictor`).
    """
    ctx = get_context("spawn")
    queue = ctx.Queue()
    proc = ctx.Process(
        target=_solve_fixed_charge_worker,
        args=(queue, candidates, total_route_mi, tank_range_mi, mpg, starting_fuel, penalty),
    )
    proc.start()
    proc.join(timeout=float(budget_seconds))

    if proc.is_alive():
        proc.terminate()
        proc.join()
        return float(budget_seconds), True

    if not queue.empty():
        status, payload = queue.get()
        if status == "error":
            raise RuntimeError(
                f"dp.solve_fixed_charge raised in the spawned child process: {payload}"
            )
        return payload, False

    # The child exited without ever putting anything on the queue (killed
    # by the OS for a reason other than our own timeout, e.g. an OOM
    # kill). Treat this conservatively as censored rather than silently
    # reporting a zero or missing time.
    return float(budget_seconds), True


@dataclass
class PredictorCellResult:
    """One measured `PREDICTOR_CELLS` row: every candidate predictor's
    value over the pruned search set, plus the measured (or censored) raw
    DP wall-clock time."""

    slug: str
    tank_range_mi: Decimal
    raw_count: int
    kept_count: int
    predictor_values: dict = field(default_factory=dict)  # name -> value
    dp_seconds: float = 0.0
    censored: bool = False


# A small, fixed number of repeats (D-19's worst-not-mean convention,
# mirrored from `measure_solver_latency`'s own default) -- kept at 2 rather
# than that command's default 3 because a cell that censors on its FIRST
# run is abandoned immediately (see `_measure_cell_dp_time` below):
# repeating a cell already known to exceed budget teaches nothing new and
# only spends more of this command's own wall-clock time, which matters
# here because `PREDICTOR_CELL_BUDGET_SECONDS` can be tens of seconds per
# attempt on the slowest pinned cells.
_MEASUREMENT_REPEATS = 2


def _measure_cell_dp_time(
    candidates, total_route_mi, tank_range_mi, mpg, starting_fuel, penalty, budget_seconds
):
    worst = 0.0
    for _ in range(_MEASUREMENT_REPEATS):
        elapsed, censored = _time_with_budget(
            budget_seconds, candidates, total_route_mi, tank_range_mi, mpg, starting_fuel, penalty
        )
        if censored:
            return budget_seconds, True
        worst = max(worst, elapsed)
    return worst, False


def _induced_threshold_and_retained(values, censored_flags, comfortable_flags):
    """Return `(threshold, retained_count)` for one predictor's values
    over the measured cells (all three lists in the same cell order).

    `threshold` is the largest ACTUALLY-OBSERVED, non-censored predictor
    value that still sits strictly below every censored cell's own value
    -- i.e. the largest threshold, expressible without inventing an
    arbitrary epsilon, under which a `value <= threshold` dispatch rule
    demotes every censored cell. Returns `(None, sum(comfortable_flags))`
    when there is nothing to demote (no cell in this run was censored).

    `retained_count` counts how many of the CELLS FLAGGED COMFORTABLE
    (measured well inside budget -- see `_COMFORTABLE_FRACTION` below)
    would still be retained (`value <= threshold`) under that induced
    threshold -- the quantity `DISPATCH_RETENTION_FLOOR` is checked
    against.
    """
    censored_values = [v for v, c in zip(values, censored_flags) if c]
    if not censored_values:
        return None, sum(comfortable_flags)

    smallest_censored = min(censored_values)
    non_censored_below = [
        v for v, c in zip(values, censored_flags) if not c and v < smallest_censored
    ]
    threshold = max(non_censored_below) if non_censored_below else smallest_censored

    retained_count = sum(
        1
        for value, comfortable in zip(values, comfortable_flags)
        if comfortable and value <= threshold
    )
    return threshold, retained_count


def _count_discordant_pairs(values, times):
    """Count pairs `(i, j)` where the predictor's own ordering and the
    measured-time ordering disagree (Kendall-style discordance). A pair
    tied on either axis is neither concordant nor discordant and is
    skipped -- ties carry no directional evidence either way."""
    discordant = 0
    n = len(values)
    for i in range(n):
        for j in range(i + 1, n):
            value_diff = values[i] - values[j]
            time_diff = times[i] - times[j]
            if value_diff == 0 or time_diff == 0:
                continue
            if (value_diff > 0) != (time_diff > 0):
                discordant += 1
    return discordant


class Command(BaseCommand):
    help = (
        "Measure every candidate predictor in "
        "routing.tests.test_dispatch_predictor.CANDIDATE_PREDICTORS "
        "(including the incumbent dp.estimate_transition_count) against "
        "measured raw DP wall-clock time on PREDICTOR_CELLS, and apply "
        "the pinned verdict rule exactly as written. Read-only beyond "
        "the seed_stations replay it triggers; no network calls; works "
        "with no routing-provider token set. Must NOT run in CI -- the "
        "figures are evidence, not a pass/fail gate; "
        "DispatchPredictorGuardTests in "
        "routing/tests/test_dispatch_predictor.py is the CI-enforcing "
        "guard that exists instead."
    )

    def handle(self, *args, **options):
        import io

        from django.core.management import call_command

        from routing.services import corridor
        from routing.services.prune import prune_dominated_candidates
        from routing.tests.test_corridor_fixtures import (
            PRICE_BASIS_NEUTRAL,
            factor_lookup_for_basis,
            load_corridor_route,
        )
        from routing.tests.test_dispatch_predictor import (
            CANDIDATE_PREDICTORS,
            CENSORED_RANK_TREATMENT,
            DISPATCH_RETENTION_FLOOR,
            PREDICTOR_CELL_BUDGET_SECONDS,
            PREDICTOR_CELLS,
            PREDICTOR_INVERSION_BUDGET,
            PREDICTOR_RANK_FLOOR,
        )
        from routing.tests.test_solver_dispatch import MPG, PENALTY, STARTING_FUEL

        self.stdout.write(
            "Rebuilding the station table from the committed CSV "
            "(manage.py seed_stations, idempotent replay, no network "
            "call)..."
        )
        call_command("seed_stations", stdout=io.StringIO())
        corridor.reset_index()
        self.stdout.write("")

        factor_for = factor_lookup_for_basis(PRICE_BASIS_NEUTRAL)

        self.stdout.write(
            self.style.SUCCESS(
                f"Vehicle (routing.tests.test_solver_dispatch): mpg={MPG}, "
                f"starting_fuel={STARTING_FUEL}, penalty={PENALTY}. "
                f"PREDICTOR_CELL_BUDGET_SECONDS={PREDICTOR_CELL_BUDGET_SECONDS}. "
                f"{len(PREDICTOR_CELLS)} cells, {len(CANDIDATE_PREDICTORS)} "
                "candidate predictors (incumbent included)."
            )
        )
        self.stdout.write("")

        results = []
        for slug, tank_range_mi in PREDICTOR_CELLS:
            route = load_corridor_route(slug)
            raw_candidates = corridor.candidates(route, factor_for=factor_for)
            search_set = prune_dominated_candidates(
                raw_candidates,
                tank_range_mi=tank_range_mi,
                total_route_mi=route.total_route_mi,
            )

            # Every predictor evaluated OUTSIDE the timed region, over the
            # SAME pruned search_set solve() itself would hand the DP --
            # never the full unpruned candidate list.
            predictor_values = {
                name: fn(
                    search_set,
                    total_route_mi=route.total_route_mi,
                    tank_range_mi=tank_range_mi,
                    starting_fuel=STARTING_FUEL,
                )
                for name, fn in CANDIDATE_PREDICTORS
            }

            dp_seconds, censored = _measure_cell_dp_time(
                search_set,
                route.total_route_mi,
                tank_range_mi,
                MPG,
                STARTING_FUEL,
                PENALTY,
                PREDICTOR_CELL_BUDGET_SECONDS,
            )

            results.append(
                PredictorCellResult(
                    slug=slug,
                    tank_range_mi=tank_range_mi,
                    raw_count=len(raw_candidates),
                    kept_count=len(search_set),
                    predictor_values=predictor_values,
                    dp_seconds=dp_seconds,
                    censored=censored,
                )
            )
            self.stdout.write(
                f"    measured {slug} @{tank_range_mi}mi: "
                f"raw={len(raw_candidates)} kept={len(search_set)} "
                f"dp_seconds={dp_seconds:.4f}"
                f"{' [CENSORED]' if censored else ''}"
            )
        self.stdout.write("")

        self._print_table(results, CANDIDATE_PREDICTORS)
        self._apply_verdict(
            results,
            CANDIDATE_PREDICTORS,
            PREDICTOR_RANK_FLOOR,
            PREDICTOR_INVERSION_BUDGET,
            DISPATCH_RETENTION_FLOOR,
            CENSORED_RANK_TREATMENT,
            PREDICTOR_CELL_BUDGET_SECONDS,
        )
        self._print_disclaimer()

    def _print_table(self, results, candidate_predictors):
        self.stdout.write(
            self.style.SUCCESS(
                "PER-CELL TABLE -- every candidate predictor's value "
                "alongside the measured (or censored) raw DP wall-clock "
                "time:"
            )
        )
        for result in results:
            self.stdout.write(
                f"    {result.slug} @{result.tank_range_mi}mi "
                f"(raw={result.raw_count} kept={result.kept_count}):"
            )
            for name, _fn in candidate_predictors:
                self.stdout.write(f"        {name}={result.predictor_values[name]}")
            censor_note = " [CENSORED at budget]" if result.censored else ""
            self.stdout.write(
                f"        measured_dp_seconds={result.dp_seconds:.4f}{censor_note}"
            )
        self.stdout.write("")

    def _apply_verdict(
        self,
        results,
        candidate_predictors,
        rank_floor,
        inversion_budget,
        retention_floor,
        censored_rank_treatment,
        budget_seconds,
    ):
        self.stdout.write(
            self.style.SUCCESS(
                "VERDICT -- applied exactly as pinned in "
                "routing.tests.test_dispatch_predictor, before any number "
                "above was seen:"
            )
        )
        self.stdout.write(
            f"    PREDICTOR_RANK_FLOOR={rank_floor}  "
            f"PREDICTOR_INVERSION_BUDGET={inversion_budget}  "
            f"DISPATCH_RETENTION_FLOOR={retention_floor}  "
            f"CENSORED_RANK_TREATMENT={censored_rank_treatment}"
        )
        self.stdout.write("")

        times = [r.dp_seconds for r in results]
        censored_flags = [r.censored for r in results]
        comfortable_flags = [
            (not r.censored) and r.dp_seconds <= (budget_seconds / 2) for r in results
        ]

        qualifying = []
        for name, _fn in candidate_predictors:
            values = [r.predictor_values[name] for r in results]

            correlation = statistics.correlation(
                [float(v) for v in values], [float(t) for t in times], method="ranked"
            )
            discordant = _count_discordant_pairs(values, times)
            threshold, retained_count = _induced_threshold_and_retained(
                values, censored_flags, comfortable_flags
            )

            clears_rank = correlation >= rank_floor
            clears_inversion = discordant <= inversion_budget
            clears_retention = (threshold is None) or (retained_count >= retention_floor)
            qualifies = clears_rank and clears_inversion and clears_retention

            if qualifies:
                qualifying.append(name)

            threshold_note = (
                "no cell needed demoting"
                if threshold is None
                else f"induced_threshold={threshold} retained={retained_count}/"
                f"{sum(comfortable_flags)} comfortable cells "
                f"(floor={retention_floor})"
            )
            self.stdout.write(
                f"    {name}: rank_correlation={correlation:.4f} "
                f"(floor={rank_floor}, {'PASS' if clears_rank else 'FAIL'})  "
                f"discordant_pairs={discordant} (budget={inversion_budget}, "
                f"{'PASS' if clears_inversion else 'FAIL'})  {threshold_note} "
                f"({'PASS' if clears_retention else 'FAIL'})  "
                f"-- {'QUALIFIES' if qualifies else 'does not qualify'}"
            )

        self.stdout.write("")
        incumbent_name = candidate_predictors[0][0]
        incumbent_verdict = "SALVAGEABLE" if incumbent_name in qualifying else "NOT SALVAGEABLE"
        self.stdout.write(
            self.style.WARNING(
                f"Incumbent ({incumbent_name}) verdict: {incumbent_verdict}."
            )
        )
        if qualifying:
            self.stdout.write(
                self.style.SUCCESS(
                    "Qualifying shortlist, in the order scored above: "
                    + ", ".join(qualifying)
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    "No member of the pinned CANDIDATE_PREDICTORS family "
                    "qualifies. This is a permitted, real result: it means "
                    "dispatch cannot be made accurate by choosing a better "
                    "scalar over this cell matrix. No predictor was added, "
                    "no floor lowered, and PREDICTOR_CELLS was not widened "
                    "in response to this outcome."
                )
            )
        self.stdout.write("")

    def _print_disclaimer(self):
        self.stdout.write(
            "This command measures raw dp.solve_fixed_charge wall-clock "
            "time on a developer workstation over committed corridor "
            "geometry, at ONE vehicle and ONE penalty. It makes NO claim "
            "about deployed-hardware time -- that is plan 18-11's "
            "measurement -- and the two must be combined before any "
            "dispatch threshold is chosen. Plan 18-12 must not adopt a "
            "threshold from this command's output alone."
        )
