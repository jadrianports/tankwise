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
"""
import io
import time
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase

from routing.services import corridor, solver
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

    **Why 5x and not tighter.** At a 5x headroom multiple over
    MEASURED_DP_SECONDS (itself the worst-of-3 observed figure for this
    exact cell), the guard trips only on an order-of-magnitude regression
    -- a real algorithmic blow-up, a lost Pareto-pruning branch, an
    accidental O(n) -> O(n^2) change -- never on ordinary machine-to-
    machine or CI-runner-to-runner noise. A tighter multiple (say 1.5x or
    2x) would flake on exactly the kind of load variance CI runners are
    known for, defeating the guard's own purpose by training engineers to
    ignore it.

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

    def test_worst_measured_corridor_solves_within_headroom(self):
        """Solving the single worst-measured corridor (Dallas -> Seattle
        @500mi) at the UI-default vehicle and the sourced $35 penalty must
        complete within MEASURED_DP_SECONDS * LATENCY_HEADROOM_MULTIPLE."""
        factor_for = factor_lookup_for_basis(OBJECTIVE_PARAMS.price_basis)
        route = load_corridor_route(_WORST_MEASURED_CORRIDOR_SLUG)
        candidates = corridor.candidates(route, factor_for=factor_for)

        started = time.perf_counter()
        solver.solve(
            candidates,
            route.total_route_mi,
            tank_range_mi=_WORST_MEASURED_TANK_RANGE_MI,
            mpg=OBJECTIVE_PARAMS.mpg,
            starting_fuel=OBJECTIVE_PARAMS.starting_fuel,
            penalty=Decimal("35"),
        )
        elapsed_s = Decimal(str(time.perf_counter() - started))

        ceiling_s = MEASURED_DP_SECONDS * LATENCY_HEADROOM_MULTIPLE
        self.assertLess(
            elapsed_s,
            ceiling_s,
            f"Solver took {elapsed_s}s, exceeding the {LATENCY_HEADROOM_MULTIPLE}x "
            f"headroom guard of {ceiling_s}s over the measured "
            f"MEASURED_DP_SECONDS={MEASURED_DP_SECONDS}s baseline -- this signals a "
            "catastrophic, order-of-magnitude latency regression, not runner noise.",
        )
