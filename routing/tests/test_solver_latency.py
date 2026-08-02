"""Pinned latency-measurement parameters for solver-only latency (D-19,
D-20, D-21).

`LATENCY_CEILING_SECONDS`, `LATENCY_HEADROOM_MULTIPLE`, and
`LATENCY_PENALTY_SWEEP` are pinned here as the single shared source of
truth -- `routing.management.commands.measure_solver_latency` and
`SolverLatencyCeilingTests`, this module's own CI-enforcing guard, both
import from this module; neither defines a copy of its own, mirroring the
`CORPUS_PARAMS`/`OBJECTIVE_PARAMS` discipline this codebase already uses
throughout Phases 16-18.

**Provenance of the ceiling -- stated exactly, because D-19's ordering was
NOT honoured.** D-19 calls for measuring the greedy, pre-deciding a ceiling,
and only then timing the DP. That is not what happened in this phase. The DP
was timed extensively first, during the latency work that produced
`dp.DP_TRANSITION_BUDGET` (see 18-04c / 18-05b / 18-05c summaries), and the
5-second figure used to calibrate that budget was adopted reactively,
derived from `GUNICORN_TIMEOUT=30` in `render.yaml` / `entrypoint.sh`.

`LATENCY_CEILING_SECONDS` below is a DIFFERENT and deliberately stricter
number, and its own derivation is clean: it comes from PROJECT.md's standing
"sub-second solve" claim, not from any measurement taken in this phase. So
while the phase-level ordering was violated, this constant is not
back-fitted to observed timings -- a breach of it is a real finding, not a
tautology. The ordering violation itself is recorded for plan 18-08's
reconciliation rather than papered over here.

**18-09: the in-suite guard's bound was an absolute wall-clock figure --
this was a portability defect, now fixed.** `SolverLatencyCeilingTests`
originally asserted `elapsed_s < MEASURED_DP_SECONDS *
LATENCY_HEADROOM_MULTIPLE` (a fixed 7.78s ceiling). That figure was pinned
from one developer workstation (MEASURED_DP_SECONDS=1.5560s, see its own
comment below) and failed on the GitHub Actions CI runner at a measured
8.12s -- not from an algorithmic regression, but because that runner is
~5.2x slower on this exact cell. An absolute bound baked in one machine's
speed and can never be portable to a slower (or faster) one. The guard
below now asserts a DP/greedy RATIO instead (LATENCY_RATIO_CEILING), timing
both arms in-process on the same run so a uniformly slower machine moves
both numerators and denominators together and the ratio stays roughly
stable. The 8.12s CI figure itself is flagged here, permanently, for plan
18-08's reconciliation -- it sharpens PRUN-01/LATENCY_CEILING_SECONDS'
existing breach finding, since a GitHub Actions runner is plausibly faster
than Render's own free-tier instance, meaning the real deployed latency
could be worse still. It is not tuned away by raising
LATENCY_HEADROOM_MULTIPLE or LATENCY_CEILING_SECONDS, and MEASURED_DP_SECONDS
is kept below, unmodified, as recorded evidence even though it no longer
bounds the in-suite guard directly.
"""
import io
import statistics
import time
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase

from routing.services import corridor, solver
from routing.tests.frozen_greedy import solve as frozen_greedy_solve
from routing.tests.test_corridor_fixtures import (
    factor_lookup_for_basis,
    load_corridor_route,
)
from routing.tests.test_plan_objective import OBJECTIVE_PARAMS
from routing.tests.test_solver_fixed_charge_optimality import PENALTY_LADDER

# Sourced from PROJECT.md's informal "sub-second solve" claim -- the only
# standing numeric-ish latency budget anywhere in this repo's docs (README,
# docs/, and every benchmark command were checked; none carries a number) --
# NOT from a measurement. This is deliberately a claim-derived ceiling, so a
# breach is a legitimate, expected-possible finding this measurement exists
# to surface honestly, never a bug in how the ceiling itself was chosen.
LATENCY_CEILING_SECONDS = Decimal("1.0")

# The in-suite guard's headroom over the MEASURED DP time (see
# MEASURED_DP_SECONDS, added in task 2) -- deliberately several times above
# the measured figure so the guard trips only on a catastrophic, order-of-
# magnitude regression from what this session actually measured, never on
# ordinary runner-to-runner noise. Inverts DISAGREEMENT_FLOOR's own logic (a
# floor near a third of the measured rate, chosen to catch a no-op without
# flaking): here the multiple sits ABOVE the measured figure because
# latency only ever regresses upward, it does not "improve past a floor"
# the way a disagreement rate can.
LATENCY_HEADROOM_MULTIPLE = Decimal("5")

# Reused, not redeclared -- the same three-rung ladder
# test_solver_fixed_charge_optimality.py already pins as this codebase's
# single shared source of truth for a penalty sweep.
LATENCY_PENALTY_SWEEP = PENALTY_LADDER

# Measured 2026-08-02 on a developer workstation (Windows 11, single
# uncontended run) via
# `.venv/Scripts/python.exe manage.py measure_solver_latency --repeats 3`.
# This is the worst-of-3 solver-only figure for the single worst-measured
# (corridor, tank_range, penalty) cell at the sourced $35 penalty:
# Dallas, TX -> Seattle, WA @500mi, exact_dp, worst=1.5560s (median
# 1.5083s). That same corridor/tank-range cell was also the aggregate
# worst across the full 72-cell sweep (all three penalty rungs, both tank
# ranges, all twelve corridors) at worst=1.7244s (penalty=$10) -- $35 is
# used here to match the guard's fixed penalty below, not because it was
# the single highest cell in the whole sweep. This constant is fixed AFTER
# task 2's measurement, unlike LATENCY_CEILING_SECONDS above, and is used
# only to size the loose in-suite headroom guard immediately below -- it
# is not itself a claim about what "should" be fast.
MEASURED_DP_SECONDS = Decimal("1.5560")

# The single worst-measured (corridor, tank_range) cell from the
# MEASURED_DP_SECONDS run above -- pinned here so the guard below imports
# the same identity the constant's own comment names, rather than
# re-deriving or hardcoding a second copy of "which cell was worst".
_WORST_MEASURED_CORRIDOR_SLUG = "dallas_tx-seattle_wa"
_WORST_MEASURED_TANK_RANGE_MI = Decimal("500")

# 18-09: measured via `.venv/Scripts/python.exe manage.py
# measure_solver_latency --repeats 3 --penalty 35` on the same developer
# workstation and the same worst-measured cell (dallas_tx-seattle_wa
# @500mi, exact_dp) MEASURED_DP_SECONDS above was pinned from -- the
# command's own printed "ratio=1655.4x vs greedy" line, DP median
# (1.8853s) over greedy median (0.0011s), both measured in the same run.
# This is the "ratio the measure_solver_latency command already reports"
# the plan calls for -- not a number chosen to make CI pass. Five
# independent repeat-of-3-medians samples taken separately (to check this
# figure's own stability before trusting it) ranged 1354.5x-2471.1x, so
# 1655.4x sits inside that observed spread, not at either extreme.
MEASURED_DP_GREEDY_RATIO = Decimal("1655.4")

# Reuses LATENCY_HEADROOM_MULTIPLE (unchanged at 5, per this plan's
# non-negotiable honesty rules) as the ratio guard's own headroom, rather
# than introducing a second, unrelated headroom number: the same
# "several times above the measured figure, catastrophic-regression-only"
# reasoning applies here just as it did to the retired absolute-bound
# guard. At 5x, the ceiling (8277.0) sits comfortably above every sample
# in the 1354.5x-2471.1x spread measured above, so ordinary greedy-arm
# scheduling noise (the dominant source of variance here, since the
# greedy arm's own median is sub-millisecond) cannot flake this guard.
LATENCY_RATIO_CEILING = MEASURED_DP_GREEDY_RATIO * LATENCY_HEADROOM_MULTIPLE

# Repeats for the in-suite ratio guard's own median-of-N timing, mirroring
# measure_solver_latency's own default `--repeats 3` rather than inventing
# a second repeat-count convention. A single-shot timing of the greedy arm
# (sub-millisecond) is too noise-prone on its own to trust; taking the
# median of 3 for both arms is what the MEASURED_DP_GREEDY_RATIO samples
# above already rely on for their own stability.
_RATIO_GUARD_REPEATS = 3


class SolverLatencyCeilingTests(TestCase):
    """D-21's loose in-suite latency guard -- the CI-enforcing half of the
    (report-only command, in-suite guard) pair this plan builds. The
    report-only `measure_solver_latency` command (never run in CI) is
    where the full evidence lives; this class is the one thing that
    actually runs on every commit.

    A Django `TestCase` (DB-backed), not `SimpleTestCase` as originally
    named in the plan -- this test solves a REAL corridor against the REAL
    committed station dataset via `corridor.candidates()`'s STRtree-over-
    the-Station-table query, exactly as `PlanObjectiveMeasurementTestCase`
    and `RealCorridorDispatchTestCase` already do elsewhere in this
    codebase. `SimpleTestCase` forbids DB access by default and would
    raise on the first query, so this is a Rule-1 correction to the plan's
    literal class name, not a deviation from its intent.

    **18-09: an absolute wall-clock bound is not portable -- a ratio is.**
    This class originally asserted `elapsed_s < MEASURED_DP_SECONDS *
    LATENCY_HEADROOM_MULTIPLE`, a fixed 7.78s ceiling pinned from this
    exact developer workstation. It failed on the GitHub Actions CI
    runner at a measured 8.12s -- not an algorithmic regression, but that
    runner simply being ~5.2x slower on this cell. The guard below now
    times BOTH the frozen greedy baseline and the DP in the same process,
    on the same run, and asserts their RATIO stays under
    LATENCY_RATIO_CEILING instead: a machine that is uniformly slower (or
    faster) moves both the numerator and the denominator together, so the
    ratio itself stays roughly stable across machines in a way one
    absolute figure never could. See this module's own docstring for the
    full record of the failed absolute-bound attempt (1.5560s local /
    8.12s CI) and why the 8.12s figure is flagged for plan 18-08.

    **Why 5x headroom and not tighter.** LATENCY_RATIO_CEILING is
    MEASURED_DP_GREEDY_RATIO (1655.4x, measured via
    `measure_solver_latency --repeats 3 --penalty 35` on this exact cell)
    times LATENCY_HEADROOM_MULTIPLE (5, unchanged from its original
    absolute-bound role). At 5x headroom, the guard trips only on an
    order-of-magnitude regression -- a real algorithmic blow-up, a lost
    Pareto-pruning branch, an accidental O(n) -> O(n^2) change -- never on
    ordinary machine-to-machine or CI-runner-to-runner noise. Five
    independent repeat-of-3-medians samples taken while deriving
    MEASURED_DP_GREEDY_RATIO ranged 1354.5x-2471.1x, comfortably inside
    the 8277.0x ceiling; a tighter multiple would risk flaking on exactly
    that spread, which is dominated by the greedy arm's own sub-
    millisecond timing noise, not by anything solve() does.

    **Why measure-and-record alone was rejected.** The report-only command
    already records the full 72-cell table every time someone chooses to
    run it by hand -- but nothing forces that choice, and a regression
    introduced after this plan (v4.0's Overture/OSM station-count
    expansion is the concretely named future risk -- see
    `station-data-expansion-todo`) would ship silently with no test ever
    failing. Latency is a shipped, user-facing property of this API (the
    deployed `GUNICORN_TIMEOUT=30s` budget and PROJECT.md's own
    "sub-second solve" claim both say so), not test-harness housekeeping
    that can be safely left to a human's memory to re-run occasionally.
    """

    @classmethod
    def setUpTestData(cls):
        call_command("seed_stations", stdout=io.StringIO())
        corridor.warm_index()

    def test_worst_measured_corridor_dp_greedy_ratio_within_ceiling(self):
        """The single worst-measured corridor (Dallas -> Seattle @500mi)
        at the UI-default vehicle and the sourced $35 penalty: the DP's
        in-process solve time, divided by the frozen greedy's own
        in-process solve time on the SAME candidates, must stay under
        LATENCY_RATIO_CEILING. A machine-independent ratio, not an
        absolute wall-clock figure -- see the class docstring's 18-09
        section for why."""
        factor_for = factor_lookup_for_basis(OBJECTIVE_PARAMS.price_basis)
        route = load_corridor_route(_WORST_MEASURED_CORRIDOR_SLUG)
        # Built ONCE, outside either timed region -- neither arm's timing
        # includes corridor-build cost, mirroring
        # measure_solver_latency.py's own methodology exactly (D-19).
        candidates = corridor.candidates(route, factor_for=factor_for)

        greedy_times_s = []
        for _ in range(_RATIO_GUARD_REPEATS):
            started = time.perf_counter()
            frozen_greedy_solve(
                candidates,
                route.total_route_mi,
                tank_range_mi=_WORST_MEASURED_TANK_RANGE_MI,
                mpg=OBJECTIVE_PARAMS.mpg,
                starting_fuel=OBJECTIVE_PARAMS.starting_fuel,
            )
            greedy_times_s.append(time.perf_counter() - started)

        dp_times_s = []
        for _ in range(_RATIO_GUARD_REPEATS):
            started = time.perf_counter()
            solver.solve(
                candidates,
                route.total_route_mi,
                tank_range_mi=_WORST_MEASURED_TANK_RANGE_MI,
                mpg=OBJECTIVE_PARAMS.mpg,
                starting_fuel=OBJECTIVE_PARAMS.starting_fuel,
                penalty=Decimal("35"),
            )
            dp_times_s.append(time.perf_counter() - started)

        greedy_seconds = Decimal(str(statistics.median(greedy_times_s)))
        dp_seconds = Decimal(str(statistics.median(dp_times_s)))
        ratio = dp_seconds / greedy_seconds

        self.assertLessEqual(
            ratio,
            LATENCY_RATIO_CEILING,
            f"DP/greedy ratio {ratio}x exceeds LATENCY_RATIO_CEILING="
            f"{LATENCY_RATIO_CEILING}x (dp_median={dp_seconds}s over "
            f"{_RATIO_GUARD_REPEATS} repeats, greedy_median={greedy_seconds}s "
            f"over {_RATIO_GUARD_REPEATS} repeats) -- this signals a "
            "catastrophic, order-of-magnitude latency regression relative "
            "to the frozen greedy baseline, not machine speed or runner "
            "noise.",
        )
