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
from decimal import Decimal

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
