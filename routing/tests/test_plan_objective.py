"""Pinned measurement parameters for plan 18-05's before/after objective
report (D-28/D-30/D-31/D-37), plus the two CI-enforcing guards that
protect the claims `measure_plan_objective` reports in full.

OBJECTIVE_PARAMS, TRIVIAL_STOP_TANK_FRACTION and DALLAS_SEATTLE_STOP_RANGE
were fixed here BEFORE any figure in this plan was measured -- neither the
command nor the guard classes below define a copy of their own; both
import from this module, the single shared source of truth, mirroring the
CORPUS_PARAMS/OBJECTIVE_PARAMS discipline Phases 16-18 have used
throughout.
"""
from dataclasses import dataclass
from decimal import Decimal

from routing.tests.test_corridor_fixtures import PRICE_BASIS_NEUTRAL


@dataclass(frozen=True)
class ObjectiveParams:
    """One pinned (vehicle, penalty, price basis) cell -- the single
    parameter set `measure_plan_objective` measures both arms at."""

    mpg: Decimal
    tank_range_mi: Decimal
    starting_fuel: Decimal
    penalty: Decimal
    price_basis: str


# Pinned before any measurement was taken (D-28/D-30). The UI-default
# vehicle (Semi, loaded: 6.5 mpg / 1,050 mi tank -- ROADMAP.md's own
# "evidence base" line), a full tank at the origin, the sourced $35
# fixed-charge penalty (18-04-SUMMARY.md's ATRI/TruckerPath derivation),
# and the neutral 1.0 price basis. Neutral is the headline because its
# meaning never drifts and is literally what `eia._frozen_table()`
# returns -- the codebase's own degradation mode, not an artificial
# choice. The `eia_fixture` basis is reported alongside, never as the
# headline. Never adjusted after seeing a measured figure.
OBJECTIVE_PARAMS = ObjectiveParams(
    mpg=Decimal("6.5"),
    tank_range_mi=Decimal("1050"),
    starting_fuel=Decimal(1),
    penalty=Decimal("35"),
    price_basis=PRICE_BASIS_NEUTRAL,
)

# ROADMAP Phase 18 criterion 1's own "under 10% of tank capacity"
# threshold, applied to gallons as a fraction of tank_range_mi / mpg.
TRIVIAL_STOP_TANK_FRACTION = Decimal("0.10")

# Criterion 1's stated Dallas -> Seattle stop-count range. Pinned here,
# before measurement, for PlanObjectiveGuardTests below. If the measured
# count falls outside this range, that is a finding to record in the
# plan's SUMMARY and hand to plan 18-08 for a ROADMAP amendment -- NOT a
# bound to widen.
DALLAS_SEATTLE_STOP_RANGE = (3, 4)
