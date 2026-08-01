"""Real-corridor regression coverage for `routing.services.dp`, added
after `224b0ee` fixed a genuine optimality bug (`_exact_sub`'s precision-
safe position-domain subtraction) that every prior differential suite in
this codebase missed.

**Why this module exists, not just another Hypothesis property.** The
bug lived in `useful_fill_levels_mi`'s `reach_mi = node_mi - position_mi`:
plain `Decimal.__sub__` silently rounds to the ambient context's 28
significant digits, and this dataset's real, Mapbox/shapely-derived
station positions routinely carry 28-29 significant digits themselves.
Every existing differential (`test_dp_differential.py`,
`test_solver_fixed_charge_optimality.py`) draws its station positions
from Hypothesis strategies over small integers/short decimals -- none of
them ever produce a position with enough decimal digits to trigger the
rounding this bug depended on. Real corridor geometry does, every time.
This module anchors the exact reproduction that surfaced the bug
(`AnchoredJacksonvilleRegressionTests`) and extends coverage to real
pruned corridor candidate sets at `starting_fuel` values other than 0
(`RealCorridorStartingFuelDifferentialTests`) -- the combination
(real-precision positions AND partial starting fuel) that produced the
original failure.

Both classes seed the real, committed station dataset via
`call_command("seed_stations")` and `corridor.warm_index()`, mirroring
`test_solver_dispatch.py`'s own `RealCorridorDispatchTestCase` pattern --
the established way this codebase exercises `corridor.candidates()`'s
DB-backed STRtree path against real corridor geometry fixtures.
"""
import io
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase

from routing.services import corridor
from routing.services.dp import COST_TOLERANCE, solve_fixed_charge
from routing.services.prune import prune_dominated_candidates
from routing.tests import frozen_greedy
from routing.tests.test_corridor_fixtures import (
    factor_lookup_for_basis,
    load_corridor_route,
)

MPG = Decimal(10)
TANK_RANGE_MI = Decimal(1050)


class RealCorridorRegressionTestCase(TestCase):
    """Seeds the real, committed station dataset once per class -- same
    pattern as `test_solver_dispatch.py`'s `RealCorridorDispatchTestCase`."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_stations", stdout=io.StringIO())
        corridor.warm_index()

    def setUp(self):
        self.factor_for = factor_lookup_for_basis("neutral")

    def _route_and_candidates(self, slug):
        route = load_corridor_route(slug)
        candidates = corridor.candidates(route, factor_for=self.factor_for)
        return route, candidates


class AnchoredJacksonvilleRegressionTests(RealCorridorRegressionTestCase):
    """Pins the exact reproduction that surfaced the `224b0ee` bug:
    `jacksonville_fl-bangor_me` at the "neutral" price basis, a 1,050 mi
    tank, `mpg=10`, `starting_fuel=0.5` (a FRACTION of tank capacity, not
    gallons), `penalty=0`, over `prune_dominated_candidates`'s output
    (347 raw candidates -> 41 kept).

    Before `224b0ee`, `useful_fill_levels_mi`'s exact-reach subtraction
    silently lost its least-significant digit on this corridor's
    28-29-significant-digit station positions, making the DP believe the
    single feasible, cheapest station (`SHEETZ #804`, opis_id 72975,
    reachable directly from START and whose own gap to FINISH also fits
    one tank) was unreachable via that level -- forcing a worse 4-stop
    plan. The fix (exact integer subtraction over each operand's own
    digit tuple) restores the correct 1-stop plan.

    Asserts stop count, the winning station's `opis_id`, and cost within
    `COST_TOLERANCE` of the known-correct value -- never exact string
    equality on the 25-decimal figure itself, so a future 1-ulp change in
    how the exact-rational money-domain comparison rounds (e.g. a further
    optimization pass touching unrelated arithmetic) does not produce a
    false alarm here.
    """

    EXPECTED_COST = Decimal("244.2417178272637795275590550")
    EXPECTED_OPIS_ID = 72975
    STARTING_FUEL = Decimal("0.5")

    def test_jacksonville_bangor_pruned_dp_finds_the_one_stop_optimum(self):
        route, candidates = self._route_and_candidates("jacksonville_fl-bangor_me")
        pruned = prune_dominated_candidates(
            candidates, tank_range_mi=TANK_RANGE_MI, total_route_mi=route.total_route_mi
        )

        plan = solve_fixed_charge(
            pruned,
            total_route_mi=route.total_route_mi,
            tank_range_mi=TANK_RANGE_MI,
            mpg=MPG,
            starting_fuel=self.STARTING_FUEL,
            penalty=Decimal(0),
        )

        context = (
            f"plan={plan!r}, raw_candidates={len(candidates)}, "
            f"kept_candidates={len(pruned)}, route_mi={route.total_route_mi}"
        )

        self.assertEqual(len(plan.stops), 1, context)
        self.assertEqual(plan.stops[0].opis_id, self.EXPECTED_OPIS_ID, context)
        self.assertLessEqual(
            abs(plan.total_cost - self.EXPECTED_COST),
            COST_TOLERANCE,
            f"total_cost {plan.total_cost} is not within COST_TOLERANCE of the "
            f"known-correct {self.EXPECTED_COST}; {context}",
        )


class RealCorridorStartingFuelDifferentialTests(RealCorridorRegressionTestCase):
    """Extends the DP-vs-frozen-greedy cost-equality regression gate
    (`test_dp_differential.py`'s `FrozenGreedyDifferentialTests`, drawn
    from small synthetic Hypothesis station sets) to real, pruned corridor
    candidate sets at `starting_fuel` values other than 0 -- the exact
    input shape (real high-precision positions plus partial starting
    fuel) that let the `224b0ee` bug through every existing differential.

    Three deliberately cheap corridors (per `18-04c-SUMMARY.md`'s own
    dispatch-calibration table, all three take the exact `exact_dp` path
    at every measured tank range, never the fallback heuristic): pruned
    candidate counts stay small enough (3-71 kept, all measured under 5s
    per solve on this dev machine) that this class runs in a few seconds
    total, not tagged `"slow"` per the existing convention
    (`test_prune_soundness.py`'s `PruneGreedyDifferentialTests`) reserved
    for the corridor-density/heavy-corridor cases.

    `frozen_greedy` -- a structurally different, walk-based algorithm,
    frozen from pre-Phase-18 `solver.py` -- is the independent referee,
    run over the UNPRUNED candidate list (its own established role
    throughout this codebase's differential suites). The DP runs over the
    PRUNED list, the real shipped path users receive. Both must agree on
    `total_cost` within `COST_TOLERANCE` at `penalty=0`, exactly the
    unconditional regression gate `FrozenGreedyDifferentialTests` already
    asserts -- extended here to real corridor density and nonzero
    starting fuel, neither of which that Hypothesis-driven suite's
    strategies ever produce.
    """

    CORRIDOR_SLUGS = (
        "houston_tx-chicago_il",
        "phoenix_az-minneapolis_mn",
        "dallas_tx-seattle_wa",
    )
    STARTING_FUEL_FRACTIONS = (Decimal("0.25"), Decimal("0.5"), Decimal("0.75"))

    def test_dp_over_pruned_real_candidates_matches_frozen_greedy_at_nonzero_starting_fuel(
        self,
    ):
        for slug in self.CORRIDOR_SLUGS:
            route, candidates = self._route_and_candidates(slug)
            pruned = prune_dominated_candidates(
                candidates,
                tank_range_mi=TANK_RANGE_MI,
                total_route_mi=route.total_route_mi,
            )
            for starting_fuel in self.STARTING_FUEL_FRACTIONS:
                with self.subTest(slug=slug, starting_fuel=starting_fuel):
                    greedy_plan = frozen_greedy.solve(
                        candidates,
                        route.total_route_mi,
                        tank_range_mi=TANK_RANGE_MI,
                        mpg=MPG,
                        starting_fuel=starting_fuel,
                    )
                    dp_plan = solve_fixed_charge(
                        pruned,
                        total_route_mi=route.total_route_mi,
                        tank_range_mi=TANK_RANGE_MI,
                        mpg=MPG,
                        starting_fuel=starting_fuel,
                        penalty=Decimal(0),
                    )

                    context = (
                        f"slug={slug}, starting_fuel={starting_fuel}, "
                        f"raw_candidates={len(candidates)}, kept_candidates={len(pruned)}, "
                        f"greedy_plan={greedy_plan!r}, dp_plan={dp_plan!r}"
                    )

                    self.assertLessEqual(
                        abs(dp_plan.total_cost - greedy_plan.total_cost),
                        COST_TOLERANCE,
                        f"total_cost differs beyond COST_TOLERANCE on real corridor "
                        f"data at nonzero starting_fuel; {context}",
                    )
                    self.assertLessEqual(
                        abs(dp_plan.total_gallons - greedy_plan.total_gallons),
                        COST_TOLERANCE,
                        f"total_gallons differs beyond COST_TOLERANCE on real "
                        f"corridor data at nonzero starting_fuel; {context}",
                    )
