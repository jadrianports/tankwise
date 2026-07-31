"""Behavioural and determinism differentials for `routing.services.dp`.

Deliberately a separate module from test_solver_fixed_charge_optimality.py
(D-07/D-09's home for the DP-vs-oracle differential) because this module
proves a different claim: at `penalty=0`, the DP is COST-equal to the
pre-Phase-18 greedy on every input -- the regression gate -- and, ONLY
when the true optimum is unique (Phase 16's `OraclePlan.is_unique_optimum`
signal at `penalty=0`), the DP is additionally PLAN-identical -- same
stops, same gallons, same cost, and every `purchase_reason` -- on both the
unpruned candidate list and Phase 17's domination-pruned one. It then
separately proves the DP's repeat-run determinism (D-14).

**Amendment, 2026-07-31 (D-36 resolution, resolving plan 18-03's reported
finding):** the original property asserted plan identity unconditionally
and failed on two Hypothesis-shrunk, hand-verified witnesses where the
DP's D-12 fewest-stops total order and the frozen greedy's own implicit
walk-order tie-break pick different, EXACTLY equal-cost plans (a genuine
design collision between two locked decisions, not a bug in either side --
see 18-03-SUMMARY.md's "Issues Encountered" for both witnesses). The claim
was over-specified: "reproduces the greedy exactly" and "uses an explicit
total-order tie-break, never last-writer-wins" cannot both hold whenever
two solvers with different tie-break rules face a genuine cost tie.
Matching the greedy's specific tie-break at `penalty=0` was rejected,
because it would create a discontinuity -- as `penalty` moves off zero,
the DP's fewest-stops plan becomes strictly better, so freezing the DP
onto the greedy's tie choice at exactly `penalty=0` would make the DP
discontinuous at the one penalty value where continuity matters most.
The narrowed claim below is what the two solvers can jointly guarantee.

`single_leg_routes()` and `COST_TOLERANCE` are imported from
test_solver_fixed_charge_optimality.py, never re-derived here (D-06) --
this module shares the oracle module's own randomized distribution rather
than building a second one.

Uses `django.test.SimpleTestCase` throughout, never Hypothesis's own
Django-integrated `TestCase`: the DP and the frozen referee are both pure
and never touch the ORM, so the Django/Hypothesis integration's
per-example database transaction would buy nothing here.
"""
from decimal import Decimal

from django.test import SimpleTestCase
from hypothesis import given, settings

from routing.services.dp import preflight_gap_check, solve_fixed_charge
from routing.services.exceptions import InfeasibleRouteError
from routing.services.prune import prune_dominated_candidates
from routing.services.solver import Candidate, PurchaseReason
from routing.tests import frozen_greedy
from routing.tests.test_solver_fixed_charge_optimality import (
    COST_TOLERANCE,
    optimal_fixed_charge_plan,
    single_leg_routes,
)


class FrozenGreedyDifferentialTests(SimpleTestCase):
    """D-07/D-09/D-16/D-36, SOLV-03 (amended 2026-07-31): at `penalty=0`,
    the DP must be COST-equal to the frozen pre-Phase-18 greedy on EVERY
    input -- this is the unconditional regression gate, and it is what
    actually catches a genuine cost regression. Additionally, ONLY when
    the true optimum is unique (gated on Phase 16's
    `OraclePlan.is_unique_optimum` signal, computed once per drawn case
    at `penalty=0` over the unpruned candidate list -- Phase 17 already
    proved prune-then-solve preserves the optimal value/set, so the same
    signal gates both arms below), the DP must additionally be
    PLAN-identical to the greedy -- stops, gallons, cost, AND every
    `purchase_reason`. "Exactly" includes the rationale (D-09) whenever
    identity is asserted at all: it costs nothing once the plans already
    match, and it catches rationale-reconstruction bugs a cost-only
    assertion cannot see.

    **Why gated, not unconditional (D-36 resolution):** the DP breaks
    ties by D-12's fewest-stops total order; the frozen greedy breaks
    ties by its own implicit walk order. On a cost-tied input the two can
    legitimately pick different plans -- this was found, not assumed: two
    Hypothesis-shrunk, hand-verified witnesses exist where both plans have
    byte-identical `total_cost` but a different station split (one witness
    even has a different stop COUNT). See `18-03-SUMMARY.md`'s "Issues
    Encountered" and this module's own `test_witness_a_...` regression
    test below, which pins one of them permanently.

    Two arms (D-16), both against the SAME `frozen_greedy.solve` referee
    over the UNPRUNED candidate list:

      - unpruned: the DP over the raw candidate list, isolating the DP so
        a failure points at one thing;
      - pruned: the DP over `prune_dominated_candidates(...)` output --
        the real shipped path users will receive. Phase 17 already proved
        prune-then-solve equals solve, so this arm is cheap.

    Also asserts the D-09 penalty-zero invariant, unconditionally, for
    every drawn case: no stop may carry the fifth `purchase_reason` wire
    string, `BYPASS_CHEAPER_NOT_WORTH_STOP` (imported from
    `routing.services.solver`, never a hand-typed literal) -- structurally
    unreachable as a winning edge at `penalty=0`, per `dp.py`'s own
    docstring. This invariant is a property of the DP's own output alone,
    not a comparison against the greedy, so it is never gated on
    uniqueness.

    Feasibility is checked symmetrically: if `frozen_greedy.solve` raises
    `InfeasibleRouteError`, `preflight_gap_check` must raise on the same
    input, and vice versa.
    """

    @given(single_leg_routes())
    @settings(deadline=None, max_examples=200)
    def test_dp_matches_frozen_greedy_at_penalty_zero(self, drawn_route):
        candidates, total_route_mi, tank_range_mi, mpg, starting_fuel = drawn_route

        try:
            greedy_plan = frozen_greedy.solve(
                candidates,
                total_route_mi,
                tank_range_mi=tank_range_mi,
                mpg=mpg,
                starting_fuel=starting_fuel,
            )
            greedy_feasible = True
        except InfeasibleRouteError:
            greedy_plan = None
            greedy_feasible = False

        retained = prune_dominated_candidates(
            candidates, tank_range_mi=tank_range_mi, total_route_mi=total_route_mi
        )

        # Computed once per drawn case, over the UNPRUNED candidate list,
        # and reused to gate the plan-identity assertion for BOTH arms
        # below (D-36 resolution, 2026-07-31). Phase 17 already proved
        # prune-then-solve preserves the true optimal value/set, so the
        # unpruned oracle's uniqueness verdict is the correct gate for the
        # pruned arm too -- recomputing it per arm would be redundant, not
        # more correct. `is_unique_optimum` is exactly the signal
        # `DpOracleDifferentialTests` (test_solver_fixed_charge_optimality.py)
        # and `MultiLegFlattenedFixedChargeTests` already gate their own
        # station-set assertions on; reused here rather than invented.
        oracle_plan = (
            optimal_fixed_charge_plan(
                candidates,
                total_route_mi,
                penalty=Decimal(0),
                tank_range_mi=tank_range_mi,
                mpg=mpg,
                starting_fuel=starting_fuel,
            )
            if greedy_feasible
            else None
        )

        for arm_name, arm_candidates in (("unpruned", candidates), ("pruned", retained)):
            try:
                preflight_gap_check(
                    arm_candidates,
                    total_route_mi=total_route_mi,
                    tank_range_mi=tank_range_mi,
                    starting_fuel=starting_fuel,
                )
                dp_feasible = True
            except InfeasibleRouteError:
                dp_feasible = False

            context = (
                f"arm={arm_name}, candidates={candidates!r}, retained={retained!r}, "
                f"total_route_mi={total_route_mi}, tank_range_mi={tank_range_mi}, "
                f"mpg={mpg}, starting_fuel={starting_fuel}, "
                f"oracle_plan={oracle_plan!r}"
            )

            self.assertEqual(
                greedy_feasible,
                dp_feasible,
                f"feasibility verdicts disagree: greedy={greedy_feasible}, "
                f"dp={dp_feasible}; {context}",
            )
            if not greedy_feasible:
                continue

            dp_plan = solve_fixed_charge(
                arm_candidates,
                total_route_mi=total_route_mi,
                tank_range_mi=tank_range_mi,
                mpg=mpg,
                starting_fuel=starting_fuel,
                penalty=Decimal(0),
            )

            # D-09 invariant: unconditional, always asserted regardless of
            # ties -- this is a property of the DP's own output alone, not
            # a comparison against the greedy.
            for dp_stop in dp_plan.stops:
                self.assertNotEqual(
                    dp_stop.purchase_reason,
                    PurchaseReason.BYPASS_CHEAPER_NOT_WORTH_STOP,
                    f"the penalty-native fifth reason fired at penalty=0, "
                    f"which the finite-fill exchange argument says is "
                    f"structurally impossible; dp_stop={dp_stop!r}; {context}",
                )

            # Cost equality: the regression gate (SOLV-03, amended
            # 2026-07-31). Asserted UNCONDITIONALLY on every input -- this
            # is what actually catches a genuine cost regression, and it
            # never depended on tie resolution in the first place.
            self.assertLessEqual(
                abs(dp_plan.total_cost - greedy_plan.total_cost),
                COST_TOLERANCE,
                f"total_cost differs beyond COST_TOLERANCE; "
                f"dp_plan={dp_plan!r}, greedy_plan={greedy_plan!r}; {context}",
            )
            self.assertLessEqual(
                abs(dp_plan.total_gallons - greedy_plan.total_gallons),
                COST_TOLERANCE,
                f"total_gallons differs beyond COST_TOLERANCE; "
                f"dp_plan={dp_plan!r}, greedy_plan={greedy_plan!r}; {context}",
            )

            # Plan identity: ONLY when the true optimum is unique (D-36
            # resolution). A cost-tied case is a genuine design collision
            # between the DP's D-12 total order and the greedy's own
            # implicit walk-order tie-break, not a defect in either side --
            # asserting identity there would ride a second correctness
            # claim (that two independently tie-broken solvers resolve
            # ties identically) on top of the cost-equality claim above,
            # exactly the flake D-13 already refuses to risk for the
            # oracle differentials.
            if oracle_plan is not None and oracle_plan.is_unique_optimum:
                self.assertEqual(
                    len(dp_plan.stops),
                    len(greedy_plan.stops),
                    f"stop count differs despite a strictly unique oracle "
                    f"optimum: dp={len(dp_plan.stops)}, "
                    f"greedy={len(greedy_plan.stops)}; dp_plan={dp_plan!r}, "
                    f"greedy_plan={greedy_plan!r}; {context}",
                )
                for dp_stop, greedy_stop in zip(dp_plan.stops, greedy_plan.stops):
                    self.assertEqual(
                        dp_stop.opis_id,
                        greedy_stop.opis_id,
                        f"opis_id differs despite a strictly unique oracle "
                        f"optimum; dp_stop={dp_stop!r}, "
                        f"greedy_stop={greedy_stop!r}; {context}",
                    )
                    self.assertEqual(
                        dp_stop.distance_from_start_mi,
                        greedy_stop.distance_from_start_mi,
                        f"distance_from_start_mi differs despite a strictly "
                        f"unique oracle optimum; dp_stop={dp_stop!r}, "
                        f"greedy_stop={greedy_stop!r}; {context}",
                    )
                    self.assertLessEqual(
                        abs(dp_stop.gallons - greedy_stop.gallons),
                        COST_TOLERANCE,
                        f"gallons differs beyond COST_TOLERANCE despite a "
                        f"strictly unique oracle optimum; dp_stop={dp_stop!r}, "
                        f"greedy_stop={greedy_stop!r}; {context}",
                    )
                    self.assertLessEqual(
                        abs(dp_stop.cost - greedy_stop.cost),
                        COST_TOLERANCE,
                        f"cost differs beyond COST_TOLERANCE despite a "
                        f"strictly unique oracle optimum; dp_stop={dp_stop!r}, "
                        f"greedy_stop={greedy_stop!r}; {context}",
                    )
                    self.assertEqual(
                        dp_stop.purchase_reason,
                        greedy_stop.purchase_reason,
                        f"purchase_reason differs despite a strictly unique "
                        f"oracle optimum; dp_stop={dp_stop!r}, "
                        f"greedy_stop={greedy_stop!r}; {context}",
                    )


class FrozenGreedyTieWitnessRegressionTests(SimpleTestCase):
    """Anchors one of plan 18-03's two reported D-36 tie-clash witnesses as
    a permanent, non-Hypothesis regression test, so the narrowed semantics
    above -- cost-equality unconditional, plan-identity gated on
    `is_unique_optimum` -- cannot be silently re-tightened back to
    unconditional plan identity by a future edit without this test
    catching it immediately, no Hypothesis shrink required.

    Witness A (the cleanest of the two -- different stop COUNT at equal
    cost), independently re-derived and hand-verified outside the test
    harness during plan 18-03: two identically-priced ($1.00/gal)
    stations at miles 1 and 3 on a 363-mile route with a 361-mile tank and
    a sliver of starting fuel. The DP's D-12 fewest-stops order prefers
    the 1-stop tied-cost plan (buy only at the farther station); the
    frozen greedy's own implicit walk order produces a different,
    equal-cost 2-stop plan (buy a little at the nearer station, top off at
    the farther one). Both total costs are byte-identical Decimals.
    """

    def test_witness_a_equal_cost_different_stop_count_is_a_genuine_tie(self):
        candidates = [
            Candidate(
                name="S0", opis_id=0, price_per_gallon=Decimal("1.00"),
                distance_from_start_mi=Decimal("1"),
            ),
            Candidate(
                name="S1", opis_id=1, price_per_gallon=Decimal("1.00"),
                distance_from_start_mi=Decimal("3"),
            ),
        ]
        total_route_mi = Decimal(363)
        tank_range_mi = Decimal(361)
        mpg = Decimal(1)
        starting_fuel = Decimal("0.01")

        greedy_plan = frozen_greedy.solve(
            candidates,
            total_route_mi,
            tank_range_mi=tank_range_mi,
            mpg=mpg,
            starting_fuel=starting_fuel,
        )
        dp_plan = solve_fixed_charge(
            candidates,
            total_route_mi=total_route_mi,
            tank_range_mi=tank_range_mi,
            mpg=mpg,
            starting_fuel=starting_fuel,
            penalty=Decimal(0),
        )
        oracle_plan = optimal_fixed_charge_plan(
            candidates,
            total_route_mi,
            penalty=Decimal(0),
            tank_range_mi=tank_range_mi,
            mpg=mpg,
            starting_fuel=starting_fuel,
        )

        context = f"dp_plan={dp_plan!r}, greedy_plan={greedy_plan!r}, oracle_plan={oracle_plan!r}"

        # The regression gate: cost equality holds even on this tied case.
        self.assertLessEqual(
            abs(dp_plan.total_cost - greedy_plan.total_cost),
            COST_TOLERANCE,
            f"witness A's costs are no longer equal -- this witness was "
            f"the D-36 finding's cleanest tie; {context}",
        )
        self.assertEqual(dp_plan.total_cost, Decimal("359.3900"), context)
        self.assertEqual(greedy_plan.total_cost, Decimal("359.3900"), context)

        # Locks in that this genuinely IS a tie -- a different stop count
        # at equal cost -- so a future change to the DP or the greedy that
        # accidentally makes them agree here does not silently invalidate
        # what this test is meant to guard. If this ever fails because the
        # plans now agree, the tie was resolved upstream and this
        # assertion (not the cost-equality one above) should be relaxed.
        self.assertNotEqual(
            len(dp_plan.stops),
            len(greedy_plan.stops),
            f"witness A no longer produces a stop-count tie clash; "
            f"{context}",
        )
        self.assertEqual(len(dp_plan.stops), 1, context)
        self.assertEqual(len(greedy_plan.stops), 2, context)

        # And the oracle's uniqueness signal must call this correctly: the
        # true optimum here is NOT unique (two distinct station sets tie),
        # which is exactly what gates the plan-identity assertion off in
        # FrozenGreedyDifferentialTests above.
        self.assertIsNotNone(oracle_plan, context)
        self.assertFalse(
            oracle_plan.is_unique_optimum,
            f"the oracle reports a unique optimum on a case with two "
            f"independently verified, differently-shaped equal-cost plans "
            f"-- is_unique_optimum is not usable as the narrowing gate; "
            f"{context}",
        )


class DpDeterminismTests(SimpleTestCase):
    """D-14's empirical half: solving one fixed, non-trivial input N>=5
    times at a fixed nonzero penalty must return byte-identical plans --
    same stop order, gallons, cost, and reasons every time.

    The structural half -- the rule that no `set` or `dict` iteration
    order may influence the recurrence -- lives in `dp.py`'s own
    "Determinism" docstring section; this test is the empirical guard on
    top of that stated rule, not a substitute for it.
    """

    def test_repeat_solves_of_a_fixed_input_are_byte_identical(self):
        # Six candidates spanning a route longer than one tank
        # (total_route_mi=1800 against tank_range_mi=500 -- 3.6 tanks),
        # forcing multiple stops so the recurrence's full relaxation
        # machinery, not just a trivial single-hop case, is exercised.
        candidates = [
            Candidate(
                name="Alpha", opis_id=1, price_per_gallon=Decimal("3.20"),
                distance_from_start_mi=Decimal(150),
            ),
            Candidate(
                name="Bravo", opis_id=2, price_per_gallon=Decimal("2.90"),
                distance_from_start_mi=Decimal(400),
            ),
            Candidate(
                name="Charlie", opis_id=3, price_per_gallon=Decimal("3.50"),
                distance_from_start_mi=Decimal(700),
            ),
            Candidate(
                name="Delta", opis_id=4, price_per_gallon=Decimal("3.00"),
                distance_from_start_mi=Decimal(950),
            ),
            Candidate(
                name="Echo", opis_id=5, price_per_gallon=Decimal("2.75"),
                distance_from_start_mi=Decimal(1250),
            ),
            Candidate(
                name="Foxtrot", opis_id=6, price_per_gallon=Decimal("3.10"),
                distance_from_start_mi=Decimal(1550),
            ),
        ]
        total_route_mi = Decimal(1800)
        tank_range_mi = Decimal(500)
        mpg = Decimal(10)
        starting_fuel = Decimal("1.00")
        penalty = Decimal("35")
        run_count = 5

        def plan_shape(plan):
            return (
                tuple(
                    (
                        stop.opis_id,
                        stop.distance_from_start_mi,
                        stop.gallons,
                        stop.cost,
                        stop.purchase_reason,
                    )
                    for stop in plan.stops
                ),
                plan.total_cost,
                plan.total_gallons,
                plan.penalised_objective,
            )

        plans = [
            solve_fixed_charge(
                candidates,
                total_route_mi=total_route_mi,
                tank_range_mi=tank_range_mi,
                mpg=mpg,
                starting_fuel=starting_fuel,
                penalty=penalty,
            )
            for _ in range(run_count)
        ]

        first_shape = plan_shape(plans[0])
        for run_index, plan in enumerate(plans[1:], start=1):
            self.assertEqual(
                plan_shape(plan),
                first_shape,
                f"run {run_index} of {run_count} produced a different plan "
                f"than run 0 on the same fixed input: run0={plans[0]!r}, "
                f"run{run_index}={plan!r}",
            )
