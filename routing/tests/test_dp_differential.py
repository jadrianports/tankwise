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

**Amendment, 2026-08-01 (Defect C -- reproducible-gate fix):**
`test_dp_matches_frozen_greedy_at_penalty_zero` drew its 200 examples from
a fresh, unseeded Hypothesis run every invocation, so two consecutive runs
of the SAME code could exercise two different sets of drawn inputs. That
made it possible for a genuine defect to pass on a lucky run and fail on
the next, and for the ROADMAP's "identical inputs return an identical plan
on repeat runs" claim to be gated by a test that was not itself
repeat-run-identical. Fixed with `@settings(..., derandomize=True)`:
Hypothesis's own documented mechanism for exactly this case -- it seeds
its internal example-generation PRNG deterministically from the test's
fully qualified name instead of the system's entropy source, so the same
200 examples are drawn on every run, every machine, forever, with no
change to `max_examples` or the `single_leg_routes()` strategy itself.
Neither `test_solver_optimality.py` nor `test_solver_fixed_charge_optimality.py`
already use `derandomize` on their own `@given` properties (checked before
choosing this route) -- their nearest precedent is `CORPUS_PARAMS`'s
`random.Random(seed=20260730)` corpus, but that seeds a hand-rolled
generator for a plain (non-Hypothesis) test, not a Hypothesis strategy, so
it is not a directly reusable mechanism here. `derandomize=True` is
Hypothesis's own first-class equivalent for a `@given`-based property and
is applied with the least change to this specific test's existing
`@settings(deadline=None, max_examples=200)` call, per this same
amendment's constraint against narrowing the strategy or lowering example
count.

**Amendment, 2026-08-01 (gate-stabilization, discovered while making the
frozen-greedy differential reproducible per the same rationale-defect-fix
pass):** a second, independent split-degeneracy tie was found once the
Hypothesis strategy was run with a fixed seed instead of a fresh one every
invocation -- `is_unique_optimum` only proves the winning STATION SET is
unique (it groups feasible subsets by `stop_opis_ids`); it says nothing
about whether the gallons purchased at each station in that set is also
forced. Reproducing witness: `candidates=[Candidate(name='S0', opis_id=0,
price_per_gallon=Decimal('1.00'), distance_from_start_mi=Decimal(1)),
Candidate(name='S1', opis_id=1, price_per_gallon=Decimal('1.00'),
distance_from_start_mi=Decimal(2))]`, `total_route_mi=102,
tank_range_mi=100, mpg=1, starting_fuel=0.01`. Both solvers buy at BOTH
stations (station set `{0, 1}` matches, `is_unique_optimum=True`) for the
SAME total cost/gallons (`101.0000`/`101.00`), but split it differently:
the DP buys 1.00 gal at S0 then 100 at S1; the greedy fills the tank
(100 gal) at S0 (its `price_here <= min(ahead prices)` check uses `<=`,
so an EQUAL price also triggers `TOP_UP_AT_CHEAPEST`) then buys only 1 gal
at S1. Both are equally optimal -- when two stations in a plan share an
identical price, the fuel-dollar cost is invariant to how a purchase is
divided between them, so the per-stop gallons/cost/reason split is a
second, independent source of non-uniqueness that `is_unique_optimum`
was never designed to see. The fix mirrors D-36's own pattern exactly:
narrow what is asserted (skip per-stop gallons/cost/reason, but keep
opis_id/distance, which stay forced regardless of price ties) rather than
patch the DP, the greedy, or the oracle's uniqueness signal. See
`EqualPriceSplitWitnessRegressionTests` for the permanently anchored,
non-Hypothesis regression test. This finding is what actually made the
Hypothesis property flake run-to-run BEFORE the fix below existed --
Defect A and Defect B's fixes alone were not sufficient to make the gate
reproducibly green, because a fresh unseeded run could draw this witness
shape on some runs and not others.

**Amendment, 2026-08-01 (rationale-defect-fix, resolving the orchestrator's
independent-verification finding on plan 18-03):** stop-for-stop identity
carves out one more narrow, documented exception on top of the D-36
uniqueness gate below: a candidate station positioned EXACTLY at
`total_route_mi` (a station that coincides with the route's finish line).
Reproducing witness: `candidates=[Candidate(name='S0', opis_id=0,
price_per_gallon=Decimal('1.00'), distance_from_start_mi=Decimal(2)),
Candidate(name='S1', opis_id=1, price_per_gallon=Decimal('1.01'),
distance_from_start_mi=Decimal(1))]`, `total_route_mi=2, tank_range_mi=20,
mpg=1, starting_fuel=0.05`. The oracle reports `is_unique_optimum=True` --
this is NOT a cost tie -- and both solvers buy an identical 1.00 gal at S1
for an identical `1.0100`; only the reason label differs: the DP says
`reach_finish`, the frozen greedy says `reach_cheaper_stop` targeting S0.

**Settled on the merits, not by preference:** the DP's `reach_finish` is
correct and the greedy's `reach_cheaper_stop` is the artifact. S0 sits at
the same coordinate as FINISH, and neither plan makes any purchase AT S0
(the reconstructed `stops` list is identical between the two solvers --
one stop, at S1). Labelling the purchase `reach_cheaper_stop` implies the
driver is routing toward a named station to do something there; that is
false here, since the trip ends at that exact coordinate and nothing is
bought at S0 in either plan. `reach_finish` names what the purchase
actually accomplishes: completing the trip. The frozen greedy's walk-based
loop structurally cannot express "the next hop is also the finish" -- its
`cheaper` branch fires whenever ANY strictly-cheaper reachable candidate
exists, with no check for whether that candidate's position happens to
equal `total_route_mi`, and `PurchaseReason.REACH_FINISH` is reachable in
the greedy only from its own separate, later branch (b), which this input
never reaches because branch (a) claims the loop first. The DP's edge
graph, by contrast, treats FINISH as a distinct synthetic node from any
same-position candidate and resolves the tie between them by insertion
order (see `dp.py`'s D-12 key), which happens to prefer the FINISH-typed
edge here -- not by design intent specific to this case, but the resulting
label is still the semantically correct one on independent inspection, so
it is kept rather than "fixed" to match the greedy.

The frozen greedy is a frozen referee (never edited) and its label cannot
be changed; the test's plan-identity assertion is narrowed instead, in the
same spirit as the D-36 uniqueness gate below -- a second, independently
verified case where the two solvers' differing internal structures produce
a legitimate labelling divergence on an otherwise-identical plan, not a
correctness bug in either. Every other per-stop field (`opis_id`,
`distance_from_start_mi`, `gallons`, `cost`) is still asserted equal for
this class; only `purchase_reason` is exempted, and only when the specific
condition below holds. See
`FinishCoincidentStationWitnessRegressionTests` for the permanently
anchored, non-Hypothesis regression test.

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


def _is_finish_coincident_reason_mismatch(
    dp_stop, greedy_stop, candidates_by_opis_id, total_route_mi
):
    """True exactly for the documented finish-coincident-station reason-
    label class (see this module's docstring amendment above): a candidate
    positioned EXACTLY at `total_route_mi` makes the DP say `reach_finish`
    and the frozen greedy say `reach_cheaper_stop` targeting that same
    candidate, on an otherwise byte-identical single purchase. Narrowly
    scoped -- both the DP's specific reason AND the greedy's specific
    reason must match this exact pattern, and the greedy's own claimed
    target must genuinely sit at the finish line, or this returns False and
    the caller's normal equality assertion still applies."""
    if dp_stop.purchase_reason == greedy_stop.purchase_reason:
        return False
    if dp_stop.purchase_reason != PurchaseReason.REACH_FINISH:
        return False
    if greedy_stop.purchase_reason != PurchaseReason.REACH_CHEAPER_STOP:
        return False
    target = candidates_by_opis_id.get(greedy_stop.reason_target_opis_id)
    return target is not None and target.distance_from_start_mi == total_route_mi


class FrozenGreedyDifferentialTests(SimpleTestCase):
    """D-07/D-09/D-16/D-36, SOLV-03 (amended 2026-07-31; restated
    2026-08-07, phase 21, D-18): at `penalty=0` AND `trust_margin=0`, the
    DP must be COST-equal to the frozen pre-Phase-18 greedy on EVERY input
    -- this is the unconditional regression gate, and it is what actually
    catches a genuine cost regression. `trust_margin=0` is asserted
    explicitly (not merely inherited from `solve_fixed_charge`'s own
    default) because the frozen greedy referee stays byte-unchanged and
    carries no trust-margin concept at all (D-18) -- proving the DP
    against it at a non-zero margin would not be proving anything; the
    margin arm is proven only against the extended fixed-charge oracle
    (`TrustMarginOracleDifferentialTests`,
    `test_solver_fixed_charge_optimality.py`), never against a referee
    that cannot express it. Additionally, ONLY when
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
    @settings(deadline=None, max_examples=200, derandomize=True)
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

        # Built once, over the UNPRUNED candidate list, for the
        # finish-coincident-station reason-label exemption below --
        # `opis_id` identifies a candidate uniquely regardless of arm, so
        # one lookup table serves both.
        candidates_by_opis_id = {c.opis_id: c for c in candidates}

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
                # D-18: explicit, not merely inherited from the default --
                # this gate's own restated claim is "penalty=0 AND
                # trust_margin=0", proven against a referee with no
                # trust-margin concept at all.
                trust_margin=Decimal(0),
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
            # 2026-07-31; restated 2026-08-07 as "penalty=0 AND
            # trust_margin=0", D-18). Asserted UNCONDITIONALLY on every
            # input -- this is what actually catches a genuine cost
            # regression, and it never depended on tie resolution in the
            # first place.
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

                # `is_unique_optimum` only proves the winning STATION SET
                # is unique (Phase 17 D-02) -- it says nothing about
                # whether the gallons purchased AT EACH station in that
                # set is also forced. When two stations in the winning set
                # share an identical price, the fuel-dollar cost is
                # invariant to how a purchase is split between them (buy
                # more at the earlier one and less at the later one, or
                # vice versa), so gallons/cost/reason can legitimately
                # differ per stop even though the station set, total cost,
                # and total gallons all agree -- see
                # `EqualPriceSplitWitnessRegressionTests` for a permanently
                # anchored witness. This is the SAME underlying
                # phenomenon as the D-36 station-set tie, one level
                # deeper: an equal price is a second, independent source
                # of a non-unique optimum that `is_unique_optimum` was
                # never designed to see (it only groups by
                # `stop_opis_ids`, never by per-stop gallons).
                stop_prices = [s.price_per_gallon for s in dp_plan.stops]
                has_equal_price_pair = len(set(stop_prices)) != len(stop_prices)

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
                    if has_equal_price_pair:
                        # Documented exemption: the winning set contains
                        # two identically-priced stations, so the exact
                        # gallons/cost/reason split is not forced by the
                        # objective -- only the station set, positions,
                        # total cost, and total gallons are (all already
                        # proven equal above/here).
                        continue
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
                    if _is_finish_coincident_reason_mismatch(
                        dp_stop, greedy_stop, candidates_by_opis_id, total_route_mi
                    ):
                        # Documented exemption (see this module's docstring
                        # amendment): a candidate sits exactly at
                        # `total_route_mi`, so the DP's `reach_finish` and
                        # the greedy's `reach_cheaper_stop` both describe
                        # the SAME single purchase -- already proven
                        # identical above on opis_id/distance/gallons/cost.
                        # Settled on the merits in favour of the DP's
                        # label; only this one field is exempted.
                        continue
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


class FinishCoincidentStationWitnessRegressionTests(SimpleTestCase):
    """Anchors the finish-coincident-station reason-label divergence (see
    this module's docstring amendment above) as a permanent, non-Hypothesis
    regression test, so the exemption in `FrozenGreedyDifferentialTests`
    cannot silently re-tighten back to unconditional reason equality
    without this test catching it immediately, no Hypothesis shrink
    required. Also proves the underlying claim that made the exemption
    correct rather than merely convenient: the oracle reports a STRICTLY
    UNIQUE optimum here (this is not a tie), and every stop field other
    than `purchase_reason` is byte-identical between the two solvers.
    """

    def test_station_at_finish_line_dp_says_reach_finish_greedy_says_reach_cheaper_stop(
        self,
    ):
        candidates = [
            Candidate(
                name="S0", opis_id=0, price_per_gallon=Decimal("1.00"),
                distance_from_start_mi=Decimal(2),
            ),
            Candidate(
                name="S1", opis_id=1, price_per_gallon=Decimal("1.01"),
                distance_from_start_mi=Decimal(1),
            ),
        ]
        total_route_mi = Decimal(2)
        tank_range_mi = Decimal(20)
        mpg = Decimal(1)
        starting_fuel = Decimal("0.05")

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

        # This is NOT a cost tie -- the oracle's optimum is strictly
        # unique, which is exactly why the reason-label divergence needs
        # its own documented exemption rather than being covered by the
        # D-36 uniqueness gate.
        self.assertIsNotNone(oracle_plan, context)
        self.assertTrue(oracle_plan.is_unique_optimum, context)

        self.assertEqual(len(dp_plan.stops), 1, context)
        self.assertEqual(len(greedy_plan.stops), 1, context)
        dp_stop, greedy_stop = dp_plan.stops[0], greedy_plan.stops[0]

        # Every other field is byte-identical -- only the reason differs.
        self.assertEqual(dp_stop.opis_id, greedy_stop.opis_id, context)
        self.assertEqual(
            dp_stop.distance_from_start_mi, greedy_stop.distance_from_start_mi, context
        )
        self.assertEqual(dp_stop.gallons, greedy_stop.gallons, context)
        self.assertEqual(dp_stop.gallons, Decimal("1.00"), context)
        self.assertEqual(dp_stop.cost, greedy_stop.cost, context)
        self.assertEqual(dp_stop.cost, Decimal("1.0100"), context)

        # The divergence this test exists to pin: settled on the merits in
        # `dp.py`'s favour, not the greedy's.
        self.assertEqual(dp_stop.purchase_reason, PurchaseReason.REACH_FINISH, context)
        self.assertEqual(
            greedy_stop.purchase_reason, PurchaseReason.REACH_CHEAPER_STOP, context
        )
        self.assertEqual(greedy_stop.reason_target_opis_id, 0, context)

        candidates_by_opis_id = {c.opis_id: c for c in candidates}
        self.assertTrue(
            _is_finish_coincident_reason_mismatch(
                dp_stop, greedy_stop, candidates_by_opis_id, total_route_mi
            ),
            f"the helper that gates the exemption in "
            f"FrozenGreedyDifferentialTests no longer recognises this "
            f"witness; {context}",
        )


class EqualPriceSplitWitnessRegressionTests(SimpleTestCase):
    """Anchors the equal-price gallons-split degeneracy (see this module's
    2026-08-01 gate-stabilization docstring amendment above) as a
    permanent, non-Hypothesis regression test. Proves the underlying claim
    that made the exemption correct: the winning station SET is unique
    (`is_unique_optimum=True`) and total cost/gallons match exactly, but
    the per-stop split legitimately differs because two stations in that
    set share an identical price.
    """

    def test_two_equal_priced_stations_split_the_purchase_differently_at_equal_cost(
        self,
    ):
        candidates = [
            Candidate(
                name="S0", opis_id=0, price_per_gallon=Decimal("1.00"),
                distance_from_start_mi=Decimal(1),
            ),
            Candidate(
                name="S1", opis_id=1, price_per_gallon=Decimal("1.00"),
                distance_from_start_mi=Decimal(2),
            ),
        ]
        total_route_mi = Decimal(102)
        tank_range_mi = Decimal(100)
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

        # The winning station SET is unique -- this is NOT a D-36 station-
        # set tie, it is the deeper split-level tie this amendment exists
        # to document.
        self.assertIsNotNone(oracle_plan, context)
        self.assertTrue(oracle_plan.is_unique_optimum, context)
        self.assertEqual(oracle_plan.stop_opis_ids, (0, 1), context)

        self.assertEqual(len(dp_plan.stops), 2, context)
        self.assertEqual(len(greedy_plan.stops), 2, context)
        self.assertEqual(
            [s.opis_id for s in dp_plan.stops], [s.opis_id for s in greedy_plan.stops], context
        )

        # Total cost/gallons are byte-identical -- the regression gate
        # this whole suite protects.
        self.assertEqual(dp_plan.total_cost, Decimal("101.0000"), context)
        self.assertEqual(greedy_plan.total_cost, Decimal("101.0000"), context)
        self.assertEqual(dp_plan.total_gallons, Decimal("101.00"), context)
        self.assertEqual(greedy_plan.total_gallons, Decimal("101.00"), context)

        # The divergence this test exists to pin: the per-stop split is
        # genuinely different, not a bug in either solver.
        dp_first, greedy_first = dp_plan.stops[0], greedy_plan.stops[0]
        self.assertNotEqual(dp_first.gallons, greedy_first.gallons, context)
        self.assertEqual(dp_first.gallons, Decimal("1.00"), context)
        self.assertEqual(greedy_first.gallons, Decimal("100.00"), context)
        self.assertEqual(dp_first.purchase_reason, PurchaseReason.REACH_CHEAPER_STOP, context)
        self.assertEqual(
            greedy_first.purchase_reason, PurchaseReason.TOP_UP_AT_CHEAPEST, context
        )

        stop_prices = [s.price_per_gallon for s in dp_plan.stops]
        self.assertNotEqual(
            len(set(stop_prices)),
            len(stop_prices),
            f"the winning set no longer contains an equal-price pair -- "
            f"the has_equal_price_pair gate in FrozenGreedyDifferentialTests "
            f"would no longer recognise this witness; {context}",
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
