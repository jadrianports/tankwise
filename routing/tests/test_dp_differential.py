"""Behavioural and determinism differentials for `routing.services.dp`.

Deliberately a separate module from test_solver_fixed_charge_optimality.py
(D-07/D-09's home for the DP-vs-oracle differential) because this module
proves a different claim: at `penalty=0`, the DP is not merely cost-equal
to the pre-Phase-18 greedy, it is PLAN-identical -- same stops, same
gallons, same cost, and every `purchase_reason` -- on both the unpruned
candidate list and Phase 17's domination-pruned one. It then separately
proves the DP's repeat-run determinism (D-14).

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
    single_leg_routes,
)


class FrozenGreedyDifferentialTests(SimpleTestCase):
    """D-07/D-09/D-16, SOLV-03: at `penalty=0`, the DP must be
    plan-identical to the frozen pre-Phase-18 greedy -- not merely
    cost-equal -- on stops, gallons, cost, AND every `purchase_reason`.
    "Exactly" includes the rationale (D-09): it costs nothing once the
    plans already match, and it catches rationale-reconstruction bugs a
    cost-only assertion cannot see.

    Two arms (D-16), both against the SAME `frozen_greedy.solve` referee
    over the UNPRUNED candidate list:

      - unpruned: the DP over the raw candidate list, isolating the DP so
        a failure points at one thing;
      - pruned: the DP over `prune_dominated_candidates(...)` output --
        the real shipped path users will receive. Phase 17 already proved
        prune-then-solve equals solve, so this arm is cheap.

    Also asserts the D-09 penalty-zero invariant: no stop may carry the
    fifth `purchase_reason` wire string, `BYPASS_CHEAPER_NOT_WORTH_STOP`
    (imported from `routing.services.solver`, never a hand-typed literal)
    -- structurally unreachable as a winning edge at `penalty=0`, per
    `dp.py`'s own docstring.

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
                f"mpg={mpg}, starting_fuel={starting_fuel}"
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

            self.assertEqual(
                len(dp_plan.stops),
                len(greedy_plan.stops),
                f"stop count differs: dp={len(dp_plan.stops)}, "
                f"greedy={len(greedy_plan.stops)}; dp_plan={dp_plan!r}, "
                f"greedy_plan={greedy_plan!r}; {context}",
            )
            for dp_stop, greedy_stop in zip(dp_plan.stops, greedy_plan.stops):
                self.assertEqual(
                    dp_stop.opis_id,
                    greedy_stop.opis_id,
                    f"opis_id differs; dp_stop={dp_stop!r}, "
                    f"greedy_stop={greedy_stop!r}; {context}",
                )
                self.assertEqual(
                    dp_stop.distance_from_start_mi,
                    greedy_stop.distance_from_start_mi,
                    f"distance_from_start_mi differs; dp_stop={dp_stop!r}, "
                    f"greedy_stop={greedy_stop!r}; {context}",
                )
                self.assertLessEqual(
                    abs(dp_stop.gallons - greedy_stop.gallons),
                    COST_TOLERANCE,
                    f"gallons differs beyond COST_TOLERANCE; dp_stop={dp_stop!r}, "
                    f"greedy_stop={greedy_stop!r}; {context}",
                )
                self.assertLessEqual(
                    abs(dp_stop.cost - greedy_stop.cost),
                    COST_TOLERANCE,
                    f"cost differs beyond COST_TOLERANCE; dp_stop={dp_stop!r}, "
                    f"greedy_stop={greedy_stop!r}; {context}",
                )
                self.assertEqual(
                    dp_stop.purchase_reason,
                    greedy_stop.purchase_reason,
                    f"purchase_reason differs; dp_stop={dp_stop!r}, "
                    f"greedy_stop={greedy_stop!r}; {context}",
                )
                self.assertNotEqual(
                    dp_stop.purchase_reason,
                    PurchaseReason.BYPASS_CHEAPER_NOT_WORTH_STOP,
                    f"the penalty-native fifth reason fired at penalty=0, "
                    f"which the finite-fill exchange argument says is "
                    f"structurally impossible; dp_stop={dp_stop!r}; {context}",
                )

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
