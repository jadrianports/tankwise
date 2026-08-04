"""D-04's anti-vacuity pair for the time-boxed DP: IDENTITY and BREACH.

A time-box is trivially satisfiable in two degenerate directions. A zero
deadline deletes the DP outright -- every attempted cell immediately
breaches and falls back, so every "the fallback works, the exception is
caught" assertion passes while the exact DP is effectively never run. An
effectively-infinite deadline disables the time-box entirely -- nothing
ever breaches, so every "the answer is unchanged" assertion passes while
the deadline does nothing at all. Neither degenerate value can satisfy
both classes below -- see each class's own docstring, and the two mutation
checks recorded in `18.1-06-SUMMARY.md`, for exactly how each half fails
the opposite degeneration.

This is the same anti-vacuity discipline `routing.tests.test_solver_
dispatch`'s `DispatchDemotionGuardTests`/`DispatchRetentionFloorGuardTests`
pair already applies one layer up (which cell reaches the exact DP at
all): a demote-everything policy passes the demotion half trivially, a
retain-everything policy passes the retention half trivially, and only
together are they non-vacuous. `DeadlineIdentityGuardTests` and
`DeadlineBreachGuardTests` below apply the identical shape one layer
down: does the clock ever change an admitted cell's answer, and does the
clock actually fire and get caught rather than leaked.

Joint unsatisfiability, stated explicitly: `DP_DEADLINE_SECONDS = 0` fails
`DeadlineIdentityGuardTests` on both the behavioural identity assertions
(a zero deadline breaches immediately, so the timed and untimed runs
never agree) and the strictly-positive closed-form assertion. An
effectively-infinite `DP_DEADLINE_SECONDS` fails `DeadlineBreachGuardTests`'s
band assertion (it sits far above the largest value the pinned
derivation could ever produce). Therefore no single deadline value
satisfies both classes, and the pair cannot be passed by deleting or
disabling the time-box.
"""
from decimal import Decimal

from routing.services import dp
from routing.services.exceptions import DeadlineExceededError
from routing.services.prune import prune_dominated_candidates
from routing.services.solver import SolverStrategy, solve
from routing.tests.test_dispatch_recovery import (
    DEADLINE_OVERSHOOT_BUDGET_SECONDS,
    HEURISTIC_FALLBACK_ALLOWANCE_SECONDS,
    RESPONSE_BAR_SECONDS,
    ROUTE_ALTERNATIVES_FANOUT,
)
from routing.tests.test_solver_dispatch import (
    MPG,
    PENALTY,
    STARTING_FUEL,
    RealCorridorDispatchTestCase,
)


class DeadlineIdentityGuardTests(RealCorridorDispatchTestCase):
    """D-04's IDENTITY half: on a cell that completes, the time-boxed DP
    returns a plan identical to an untimed run -- the clock never changes
    an answer, only whether there is one. Paired with
    `DeadlineBreachGuardTests` below -- either guard alone is satisfiable
    vacuously (a zero deadline passes no assertion in `Deadline
    BreachGuardTests` that checks the deadline is positive, but a
    would-be "IDENTITY-only" suite with no BREACH counterpart could not
    tell a deleted time-box from a working one); together they are not.
    This class alone cannot prove the deadline ever actually fires --
    that is `DeadlineBreachGuardTests`'s job.
    """

    def test_timed_and_untimed_solve_agree_on_a_cell_that_completes(self):
        # sacramento_ca-salt_lake_city_ut at the API-default vehicle
        # (solve()'s own defaults: mpg=10, tank_range_mi=500,
        # starting_fuel=1) is the one cell with genuine deployed-hardware
        # evidence of exact_dp running in single-digit milliseconds
        # (18-11-SUMMARY.md: 5.0-5.3ms) -- admitted comfortably, and
        # cannot become slow or flaky.
        route, candidates = self._route_and_candidates(
            "sacramento_ca-salt_lake_city_ut"
        )
        untimed = solve(candidates, route.total_route_mi, penalty=PENALTY, deadline=None)
        # No explicit deadline= -- inherits the real, production
        # dp.DP_DEADLINE_SECONDS via solve()'s own default.
        timed = solve(candidates, route.total_route_mi, penalty=PENALTY)

        self.assertEqual(timed.strategy, SolverStrategy.EXACT_DP)
        self.assertFalse(timed.deadline_breached)
        self.assertEqual(untimed.strategy, timed.strategy)
        self.assertEqual(untimed.total_cost, timed.total_cost)
        self.assertEqual(untimed.total_gallons, timed.total_gallons)
        self.assertEqual(
            [(s.opis_id, s.gallons, s.cost) for s in untimed.stops],
            [(s.opis_id, s.gallons, s.cost) for s in timed.stops],
        )

    def test_identity_holds_on_a_second_independent_admitted_cell(self):
        # nashville_tn-buffalo_ny@500mi estimates 8,168 transitions
        # (ADMISSION_MANIFEST, test_solver_dispatch.py) -- comfortably
        # admitted (~16% of dp.DP_TRANSITION_BUDGET's 50,000) and measured
        # at 16ms under the real, production dp.DP_DEADLINE_SECONDS -- so
        # this is not a single-cell accident.
        #
        # Deliberately NOT houston_tx-chicago_il@1050mi (the plan's
        # originally named second cell, estimate 23): measured directly,
        # its actual DP run examines far fewer than
        # dp._DEADLINE_CHECK_STRIDE (500) (state, level) pairs, so the
        # deadline's stride-checked code path never fires there no matter
        # what dp.DP_DEADLINE_SECONDS is set to -- it cannot participate in
        # this class's own anti-vacuity mutation check (see
        # 18.1-06-SUMMARY.md for the measured evidence). Same fact applies
        # to sacramento_ca-salt_lake_city_ut above (estimate 124) -- kept
        # regardless, for its deployed-hardware-evidence value, but this
        # second test is the one this class relies on to prove the check
        # actually fires on a real corridor.
        route, candidates = self._route_and_candidates("nashville_tn-buffalo_ny")
        untimed = solve(
            candidates,
            route.total_route_mi,
            tank_range_mi=Decimal(500),
            mpg=MPG,
            starting_fuel=STARTING_FUEL,
            penalty=PENALTY,
            deadline=None,
        )
        timed = solve(
            candidates,
            route.total_route_mi,
            tank_range_mi=Decimal(500),
            mpg=MPG,
            starting_fuel=STARTING_FUEL,
            penalty=PENALTY,
        )

        self.assertEqual(timed.strategy, SolverStrategy.EXACT_DP)
        self.assertFalse(timed.deadline_breached)
        self.assertEqual(untimed.strategy, timed.strategy)
        self.assertEqual(untimed.total_cost, timed.total_cost)
        self.assertEqual(untimed.total_gallons, timed.total_gallons)
        self.assertEqual(
            [(s.opis_id, s.gallons, s.cost) for s in untimed.stops],
            [(s.opis_id, s.gallons, s.cost) for s in timed.stops],
        )

    def test_the_pinned_deadline_is_strictly_positive(self):
        # The closed-form half that catches the zero-deadline degenerate
        # directly, rather than only through a behavioural consequence
        # (a zero deadline breaches every admitted cell immediately,
        # which the two tests above would also catch -- this assertion
        # makes the failure mode explicit and independent of any
        # particular corridor).
        self.assertGreater(
            dp.DP_DEADLINE_SECONDS,
            0,
            "dp.DP_DEADLINE_SECONDS must be strictly positive -- a zero "
            "or negative deadline deletes the exact DP outright.",
        )
