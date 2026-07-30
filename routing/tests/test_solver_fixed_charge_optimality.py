"""Independent brute-force oracle for the fuel + penalty*stops objective.

The optimum this module computes is: the plan that minimizes fuel dollars
plus penalty times the count of stations where gallons purchased is greater
than zero, for a given route.

The optimum is found by enumerating every subset of candidate stations and
costing each one fully, in a plain itertools.combinations loop -- there is
no memo table, no state-keyed dictionary, and no (node, fuel) recurrence
anywhere in this module. A reviewer checking this module's independence
from the DP it will later judge can do so by reading that loop.

The only symbols this module imports from the routing.services package are
Candidate and solve, plus InfeasibleRouteError from
routing.services.exceptions, needed only to turn an infeasible route into a
boolean feasibility verdict for the penalty=0 anchor property below. Every
other solver helper -- the plan and stop dataclasses, the purchase-reason
constants, and the solver's own private helpers -- is deliberately not
imported here.

Tests use django.test.SimpleTestCase together with Hypothesis's @given,
never hypothesis.extra.django.TestCase: the solver under comparison is pure
and never touches the ORM, so the Django test-case integration would buy a
per-example database transaction for nothing.

This oracle is deliberately exponential and can never ship in production:
on the real ~508-candidate Dallas-Seattle corridor, subset enumeration is
2**508. It terminates in this test suite only because MAX_STATIONS bounds
how many candidate stations a single example may generate.
"""
from dataclasses import dataclass
from decimal import Decimal
from itertools import chain, combinations

from django.test import SimpleTestCase
from hypothesis import given, settings
from hypothesis import strategies as st

from routing.services import Candidate, solve
from routing.services.exceptions import InfeasibleRouteError

# The D-12 tuning knob. Pre-authorized to step 6 -> 5 -> 4 if a measured
# runtime breaches the ~30s ceiling for the oracle test classes. The
# 200-example count on the anchor property below is fixed and is NOT a
# knob -- ROADMAP criterion 1 names "the full 200-case Hypothesis run"
# explicitly, so trading examples away would quietly renegotiate it.
MAX_STATIONS = 6

# D-09's tolerance band: both sides accumulate purchases in a different
# order, and Decimal division at the default 28-digit context is inexact,
# so exact equality is not the right assertion. Four orders of magnitude
# tighter than a cent, so this band cannot mask a real optimality bug.
COST_TOLERANCE = Decimal("0.0001")

# The $35 fixed-charge penalty default, traced to ATRI operating-cost data
# (see STATE.md). Test bodies pass penalty explicitly; this is a
# convenience default for later test classes in this module.
DEFAULT_PENALTY = Decimal("35")

# D-10's sweep values: three strictly increasing penalties, the last being
# the sourced $35 figure. Deliberately short -- every rung costs one more
# full oracle solve per Hypothesis example, and FixedChargePenaltyConsistencyTests
# already solves the same drawn route at every rung.
PENALTY_LADDER = (Decimal(0), Decimal("10"), DEFAULT_PENALTY)


@dataclass(frozen=True)
class OraclePlan:
    """The oracle's answer for one route at one penalty.

    objective is the equality target for comparisons against another
    solver's result. fuel_cost is the fuel-dollars-only figure Phase 18's
    INTG-02 check needs (total_cost must report fuel dollars only, never
    the penalised total). stop_opis_ids is the station set Phase 17's
    prune property will compare against. gallons makes the D-04
    nonzero-purchase rule inspectable -- every entry is strictly positive
    by construction.

    stop_opis_ids is in increasing distance_from_start_mi order; gallons
    is index-aligned with it. Deliberately does not mirror solver.py's
    FuelPlan/FuelStop shape -- those dataclasses are Phase 18's to change,
    and the oracle must not be coupled to them.
    """

    objective: Decimal
    fuel_cost: Decimal
    stop_opis_ids: tuple[int, ...]
    gallons: tuple[Decimal, ...]

    @property
    def stop_count(self) -> int:
        return len(self.stop_opis_ids)


def _subsets_by_distance(candidates):
    """Every subset of candidates, pre-sorted by
    (distance_from_start_mi, opis_id), yielded already in increasing-
    distance order. r=0 yields the empty subset -- the "reach the finish
    on starting fuel alone" case.

    itertools.combinations emits tuples in the input iterable's order, so
    sorting once up front means every yielded subset is already in
    increasing-distance order, which is also exactly the ordering D-08's
    tie-break needs.
    """
    ordered = sorted(candidates, key=lambda c: (c.distance_from_start_mi, c.opis_id))
    return chain.from_iterable(
        combinations(ordered, r) for r in range(len(ordered) + 1)
    )


def _useful_fill_levels_mi(remaining_nodes_mi, position_mi, fuel_on_arrival_mi, tank_range_mi):
    """Return the deduplicated, sorted useful purchase amounts (in miles of
    range bought) at one station in a fixed subset, given that subset's own
    remaining nodes ahead of this position.

    remaining_nodes_mi holds the distances of the fixed subset's own later
    members plus the finish -- never the whole candidate pool. Scoping to
    the whole pool would multiply the branching factor across every
    enumerated subset and blow the D-12 runtime budget; scoping to the
    subset's own remaining members plus FINISH keeps the useful-amount set
    small and finite.

    The candidate amounts are: for each remaining node, the range that
    lands exactly on that node (node_distance - position_mi -
    fuel_on_arrival_mi); plus the amount that fills the tank to capacity
    (tank_range_mi - fuel_on_arrival_mi). Both are filtered to the
    half-open interval (0, tank_range_mi - fuel_on_arrival_mi] -- strictly
    positive, because a zero purchase is never useful here: the module-
    level D-04 rule treats a station forced to buy zero as a degenerate
    duplicate of the smaller subset that omits it, and that smaller subset
    is enumerated separately by _subsets_by_distance.

    Proof this set is exhaustive over optima, for a fixed subset in which
    every member buys strictly more than zero: take any optimal
    assignment. Suppose some station's purchase amount is neither "exactly
    enough to reach the next node at which a purchase happens or the
    finish" nor "fill to capacity". Then there is slack in both
    directions, so perturb by a small positive epsilon. If this station's
    price is strictly below the price at the next purchase point, buy
    epsilon more here and epsilon less there -- possible because the tank
    is not yet full here and there is slack to give up there -- and total
    cost strictly falls, contradicting optimality. If the price here is
    strictly above the next purchase point's price, buy epsilon less here
    and epsilon more there -- possible because we are above the
    exact-reach amount here -- and total cost strictly falls, again a
    contradiction. If the two prices are equal, the perturbation is
    cost-neutral, so an optimum also exists at one of the interval's
    endpoints. Either way, an optimum exists at one of the finitely many
    listed amounts.

    Penalty-specific extension, which is why this lemma is re-derived here
    rather than imported from the pure-fuel oracle: the perturbation above
    never changes the penalty term, because the subset -- and therefore
    the stop count -- is held fixed throughout the argument, and every
    purchase in the perturbation stays strictly positive by construction.
    The only way the stop count could change is a purchase driven all the
    way to exactly zero, and that possibility is not lost: it is exactly
    the smaller subset that omits this station, which the caller
    enumerates separately and which pays one fewer penalty charge. So,
    conditional on a fixed subset, minimizing fuel dollars plus penalty
    times stop count is exactly minimizing fuel dollars, and the penalty
    enters the objective once, outside this inner enumeration.
    """
    cap_mi = tank_range_mi - fuel_on_arrival_mi
    levels = set()
    for node_mi in remaining_nodes_mi:
        exact_reach_mi = node_mi - position_mi - fuel_on_arrival_mi
        if exact_reach_mi > 0:
            levels.add(exact_reach_mi)
    if cap_mi > 0:
        levels.add(cap_mi)
    return sorted(level_mi for level_mi in levels if 0 < level_mi <= cap_mi)


def _cheapest_fuel_cost_for_subset(subset, total_route_mi, tank_range_mi, mpg, starting_fuel):
    """Return (fuel_cost, gallons) for the cheapest feasible way to fuel
    this fixed, distance-ordered subset such that every member buys
    strictly more than zero gallons, or None if no such assignment reaches
    the finish.

    Enumerated exhaustively as an iterative frontier of complete partial
    assignments, expanded one subset position at a time -- never merged,
    deduplicated, or keyed by position and fuel level. Every partial
    assignment survives independently through the whole expansion; the
    absence of any state-keyed dictionary here is exactly what keeps this
    oracle's subset loop reviewer-legible per D-01.

    Deliberately does not use the classic gas-station greedy per subset,
    even though it is provably optimal for a mandated station set --
    reimplementing that reasoning at the inner loop would reintroduce the
    shared mental model the phase boundary exists to prevent. This
    function stays dumb end to end: it tries every useful amount at every
    position and keeps every surviving partial assignment.

    START is a non-purchasable node: the origin can never be billed for
    fuel, so the initial frontier's fuel is bounded by the fuel actually
    on board (starting_fuel * tank_range_mi). At every real station in the
    subset the bound is tank_range_mi, because the tank can be topped off
    there.
    """
    # Each frontier entry: (position_mi, fuel_mi, cost, gallons_tuple).
    frontier = [(Decimal(0), starting_fuel * tank_range_mi, Decimal(0), ())]

    for i, station in enumerate(subset):
        remaining_nodes_mi = [s.distance_from_start_mi for s in subset[i + 1 :]]
        remaining_nodes_mi.append(total_route_mi)

        next_frontier = []
        for position_mi, fuel_mi, cost, gallons in frontier:
            gap_mi = station.distance_from_start_mi - position_mi
            if gap_mi > fuel_mi:
                continue  # cannot physically reach this station
            fuel_on_arrival_mi = fuel_mi - gap_mi
            for level_mi in _useful_fill_levels_mi(
                remaining_nodes_mi, station.distance_from_start_mi, fuel_on_arrival_mi, tank_range_mi
            ):
                purchase_gallons = level_mi / mpg
                next_frontier.append(
                    (
                        station.distance_from_start_mi,
                        fuel_on_arrival_mi + level_mi,
                        cost + purchase_gallons * station.price_per_gallon,
                        gallons + (purchase_gallons,),
                    )
                )
        frontier = next_frontier
        if not frontier:
            return None

    best = None
    for position_mi, fuel_mi, cost, gallons in frontier:
        gap_mi = total_route_mi - position_mi
        if gap_mi <= fuel_mi and (best is None or cost < best[0]):
            best = (cost, gallons)
    return best


def optimal_fixed_charge_plan(
    candidates,
    total_route_mi,
    *,
    penalty,
    tank_range_mi=Decimal(500),
    mpg=Decimal(10),
    starting_fuel=Decimal(1),
) -> "OraclePlan | None":
    """Return the OraclePlan minimizing fuel dollars plus penalty times
    stop count, over every subset of candidates, or None if no subset
    yields a feasible plan.

    penalty is a required keyword-only plain function argument -- this
    oracle never reads a Django setting; that seam belongs to Phase 18's
    INTG-01. Per D-06, infeasibility falls purely out of the enumeration:
    there is no pre-flight gap check anywhere in this module, so this
    oracle and a DP's own gap check can never agree merely because they
    share a reachability rule.

    Ties are broken by an explicit total order (D-08): lowest objective,
    then fewest stops, then the sorted tuple of distance_from_start_mi
    (already the natural order of a subset from _subsets_by_distance),
    then the tuple of opis_id. Deterministic across Hypothesis seeds and
    repeat runs -- there is no last-writer-wins path.
    """
    best_plan = None
    best_key = None
    for subset in _subsets_by_distance(candidates):
        result = _cheapest_fuel_cost_for_subset(subset, total_route_mi, tank_range_mi, mpg, starting_fuel)
        if result is None:
            continue
        fuel_cost, gallons = result
        stop_opis_ids = tuple(station.opis_id for station in subset)
        distances_mi = tuple(station.distance_from_start_mi for station in subset)
        objective = fuel_cost + penalty * len(subset)
        key = (objective, len(subset), distances_mi, stop_opis_ids)
        if best_key is None or key < best_key:
            best_key = key
            best_plan = OraclePlan(
                objective=objective,
                fuel_cost=fuel_cost,
                stop_opis_ids=stop_opis_ids,
                gallons=gallons,
            )
    return best_plan


def _candidates_from_tuples(station_tuples, total_route_mi):
    """Build Candidate objects from drawn (price, distance) tuples,
    dropping any tuple whose distance exceeds total_route_mi. Dropping --
    not raising -- keeps generated inputs inside solve()'s own contract:
    an out-of-range candidate would trigger InvalidRouteInputError, which
    is input validation on the caller's contract, not the optimality
    property under test here.
    """
    candidates = []
    for i, (price, dist) in enumerate(station_tuples):
        dist_mi = Decimal(dist)
        if dist_mi <= total_route_mi:
            candidates.append(
                Candidate(
                    name=f"S{i}",
                    opis_id=i,
                    price_per_gallon=price,
                    distance_from_start_mi=dist_mi,
                )
            )
    return candidates


@st.composite
def single_leg_routes(draw):
    """Draw a single-leg route: a candidate list plus the four scalar
    solve()/optimal_fixed_charge_plan() arguments, as Decimals throughout.
    """
    total_route_mi = Decimal(draw(st.integers(min_value=1, max_value=800)))
    station_tuples = draw(
        st.lists(
            st.tuples(
                st.decimals(min_value=Decimal("1.00"), max_value=Decimal("6.00"), places=2),
                st.integers(min_value=1, max_value=800),
            ),
            max_size=MAX_STATIONS,
            unique_by=lambda t: t[1],
        )
    )
    tank_range_mi = Decimal(draw(st.integers(min_value=20, max_value=800)))
    mpg = Decimal(draw(st.integers(min_value=1, max_value=50)))
    starting_fuel = draw(st.decimals(min_value=Decimal("0.00"), max_value=Decimal("1.00"), places=2))
    candidates = _candidates_from_tuples(station_tuples, total_route_mi)
    return candidates, total_route_mi, tank_range_mi, mpg, starting_fuel


@st.composite
def flattened_multi_leg_routes(draw):
    """Draw a flattened multi-leg route: a candidate list, the flattened
    total_route_mi, the leg boundary offsets, and the four scalar
    solve()/optimal_fixed_charge_plan() arguments.

    Mirrors the production flattening rule in routing/services/multi_leg.py
    (permitted read for this rule; the shipped oracle in
    test_solver_optimality.py is not, per D-05/D-18): each leg gets its
    own local (price, local_distance) station tuples, and every leg's
    local distances are offset-summed onto one continuous scale by
    adding the cumulative length of all prior legs -- never a merged
    multi-leg line, never solved leg by leg. The oracle and the shipped
    greedy both see exactly one flattened candidate list on one
    continuous distance scale.

    Uses @st.composite rather than chained flatmap calls: each leg's
    offset depends on values drawn for prior legs, and composite keeps
    the whole flattened structure as one strategy Hypothesis can shrink
    holistically -- disconnected per-leg lists summed in the test body
    would forfeit shrinking across the leg boundary.

    The flattened candidate count is bounded to MAX_STATIONS by dividing
    across legs (max(1, MAX_STATIONS // num_legs) per leg) and by
    truncating the flattened list to MAX_STATIONS entries before
    returning -- never by adding. The oracle's cost is a function of
    total candidate count, not leg count, so three legs of MAX_STATIONS
    stations each would be MAX_STATIONS=18 in disguise and blow the
    D-12 runtime budget by orders of magnitude.
    """
    num_legs = draw(st.integers(min_value=2, max_value=3))
    per_leg_cap = max(1, MAX_STATIONS // num_legs)

    offset_mi = Decimal(0)
    leg_boundaries_mi = [offset_mi]
    flattened_tuples = []
    for _ in range(num_legs):
        leg_len_mi = draw(st.integers(min_value=200, max_value=800))
        local_tuples = draw(
            st.lists(
                st.tuples(
                    st.decimals(min_value=Decimal("1.00"), max_value=Decimal("6.00"), places=2),
                    # strictly inside this leg -- never on or past its far boundary
                    st.integers(min_value=1, max_value=leg_len_mi - 1),
                ),
                max_size=per_leg_cap,
                unique_by=lambda t: t[1],
            )
        )
        for price, local_dist_mi in local_tuples:
            flattened_tuples.append((price, offset_mi + Decimal(local_dist_mi)))
        offset_mi += Decimal(leg_len_mi)
        leg_boundaries_mi.append(offset_mi)

    total_route_mi = offset_mi
    # Truncate to MAX_STATIONS -- the flattened total, not the per-leg
    # caps alone, is the bound the oracle's subset enumeration must obey.
    flattened_tuples = flattened_tuples[:MAX_STATIONS]
    candidates = _candidates_from_tuples(flattened_tuples, total_route_mi)

    tank_range_mi = Decimal(draw(st.integers(min_value=20, max_value=800)))
    mpg = Decimal(draw(st.integers(min_value=1, max_value=50)))
    starting_fuel = draw(st.decimals(min_value=Decimal("0.00"), max_value=Decimal("1.00"), places=2))
    return candidates, total_route_mi, tank_range_mi, mpg, starting_fuel, leg_boundaries_mi


class FixedChargeOracleAnchorTests(SimpleTestCase):
    """The D-09 / ROADMAP-criterion-1 anchor: at penalty=0, this oracle
    must agree with the shipped greedy solve() on feasibility and on fuel
    cost, across the full fixed 200-example Hypothesis run.
    """

    @given(single_leg_routes())
    @settings(deadline=None, max_examples=200)
    def test_penalty_zero_anchors_to_shipped_greedy(self, drawn_route):
        candidates, total_route_mi, tank_range_mi, mpg, starting_fuel = drawn_route

        try:
            greedy_plan = solve(
                candidates,
                total_route_mi,
                tank_range_mi=tank_range_mi,
                mpg=mpg,
                starting_fuel=starting_fuel,
            )
            greedy_feasible, greedy_fuel_cost = True, greedy_plan.total_cost
        except InfeasibleRouteError:
            greedy_feasible, greedy_fuel_cost = False, None

        oracle_plan = optimal_fixed_charge_plan(
            candidates,
            total_route_mi,
            penalty=Decimal(0),
            tank_range_mi=tank_range_mi,
            mpg=mpg,
            starting_fuel=starting_fuel,
        )
        oracle_feasible = oracle_plan is not None

        context = (
            f"candidates={candidates!r}, total_route_mi={total_route_mi}, "
            f"tank_range_mi={tank_range_mi}, mpg={mpg}, starting_fuel={starting_fuel}"
        )
        self.assertEqual(
            greedy_feasible,
            oracle_feasible,
            f"feasibility verdicts disagree: greedy={greedy_feasible}, "
            f"oracle={oracle_feasible}; {context}",
        )
        if greedy_feasible:
            self.assertEqual(
                oracle_plan.objective,
                oracle_plan.fuel_cost,
                f"oracle objective must equal fuel_cost at penalty=0; {context}",
            )
            self.assertLessEqual(
                abs(oracle_plan.fuel_cost - greedy_fuel_cost),
                COST_TOLERANCE,
                f"oracle fuel_cost={oracle_plan.fuel_cost} vs greedy "
                f"total_cost={greedy_fuel_cost} exceeds tolerance; {context}",
            )


class FixedChargePenaltyConsistencyTests(SimpleTestCase):
    """D-10: non-zero penalties are exercised by oracle-vs-oracle self-
    consistency properties, since the shipped greedy is penalty-blind and
    no DP exists yet. Every assertion compares the oracle's own
    recomputed values across PENALTY_LADDER on the SAME drawn route --
    never an oracle result at one penalty against a solve() result at a
    different penalty. solve() is fixed-objective (pure fuel dollars) and
    is only a valid comparison point at penalty=0; that comparison is
    FixedChargeOracleAnchorTests above.

    max_examples=50 is a discretionary formulation choice for this class
    only (16-CONTEXT.md grants the precise formulation of the D-10
    properties beyond the two named): each example already performs
    three full oracle solves (one per PENALTY_LADDER rung), so 50
    examples is 150 oracle solves per test method, roughly three
    quarters of one anchor class's cost. This is NOT a lowering of the
    anchor classes' max_examples=200, which stays fixed by ROADMAP
    criterion 1.

    Inequality assertions need no tolerance band except in the fuel-cost
    property below, because >= and <= already absorb summation-order
    noise.
    """

    def _plans_by_rung(self, drawn_route):
        candidates, total_route_mi, tank_range_mi, mpg, starting_fuel = drawn_route
        return {
            penalty: optimal_fixed_charge_plan(
                candidates,
                total_route_mi,
                penalty=penalty,
                tank_range_mi=tank_range_mi,
                mpg=mpg,
                starting_fuel=starting_fuel,
            )
            for penalty in PENALTY_LADDER
        }

    @given(single_leg_routes())
    @settings(deadline=None, max_examples=50)
    def test_feasibility_is_penalty_invariant(self, drawn_route):
        """Property 5: plan(p) is None iff plan(0) is None, for every
        rung. Charging for stops can never make a reachable route
        unreachable -- if this ever fails, the subset enumeration is
        dropping feasible plans. Checked first and unconditionally (no
        skip) so a silent skip in the other methods below can never hide
        a genuine feasibility disagreement.
        """
        plans = self._plans_by_rung(drawn_route)
        zero_feasible = plans[Decimal(0)] is not None
        for penalty, plan in plans.items():
            self.assertEqual(
                plan is not None,
                zero_feasible,
                f"feasibility at penalty={penalty} disagrees with the "
                f"penalty=0 verdict={zero_feasible}; drawn_route={drawn_route!r}",
            )

    @given(single_leg_routes())
    @settings(deadline=None, max_examples=50)
    def test_stop_count_non_increasing_as_penalty_rises(self, drawn_route):
        """Property 1 (D-10, named): for consecutive rungs p_lo < p_hi,
        plan(p_hi).stop_count <= plan(p_lo).stop_count. Raising the price
        of a stop can never make the optimum want more stops."""
        plans = self._plans_by_rung(drawn_route)
        if plans[Decimal(0)] is None:
            return  # feasibility invariant is asserted separately above
        for p_lo, p_hi in zip(PENALTY_LADDER, PENALTY_LADDER[1:]):
            self.assertLessEqual(
                plans[p_hi].stop_count,
                plans[p_lo].stop_count,
                f"stop_count rose from penalty={p_lo} "
                f"({plans[p_lo].stop_count}) to penalty={p_hi} "
                f"({plans[p_hi].stop_count}); drawn_route={drawn_route!r}",
            )

    @given(single_leg_routes())
    @settings(deadline=None, max_examples=50)
    def test_objective_at_least_penalty_zero_objective(self, drawn_route):
        """Property 2 (D-10, named): for every rung p,
        plan(p).objective >= plan(0).objective."""
        plans = self._plans_by_rung(drawn_route)
        if plans[Decimal(0)] is None:
            return
        zero_objective = plans[Decimal(0)].objective
        for penalty in PENALTY_LADDER:
            self.assertGreaterEqual(
                plans[penalty].objective,
                zero_objective,
                f"objective at penalty={penalty} "
                f"({plans[penalty].objective}) fell below the penalty=0 "
                f"objective ({zero_objective}); drawn_route={drawn_route!r}",
            )

    @given(single_leg_routes())
    @settings(deadline=None, max_examples=50)
    def test_objective_non_decreasing_across_ladder(self, drawn_route):
        """Property 3 (discretionary generalization of property 2): for
        consecutive rungs, plan(p_hi).objective >= plan(p_lo).objective."""
        plans = self._plans_by_rung(drawn_route)
        if plans[Decimal(0)] is None:
            return
        for p_lo, p_hi in zip(PENALTY_LADDER, PENALTY_LADDER[1:]):
            self.assertGreaterEqual(
                plans[p_hi].objective,
                plans[p_lo].objective,
                f"objective fell from penalty={p_lo} "
                f"({plans[p_lo].objective}) to penalty={p_hi} "
                f"({plans[p_hi].objective}); drawn_route={drawn_route!r}",
            )

    @given(single_leg_routes())
    @settings(deadline=None, max_examples=50)
    def test_fuel_cost_non_decreasing_as_penalty_rises(self, drawn_route):
        """Property 4 (discretionary): for consecutive rungs,
        plan(p_hi).fuel_cost >= plan(p_lo).fuel_cost - COST_TOLERANCE. The
        penalty=0 plan minimizes fuel alone, so trading stops away for a
        higher penalty can only ever cost more fuel. The tolerance band
        absorbs summation-order noise only; it is not slack in the claim."""
        plans = self._plans_by_rung(drawn_route)
        if plans[Decimal(0)] is None:
            return
        for p_lo, p_hi in zip(PENALTY_LADDER, PENALTY_LADDER[1:]):
            self.assertGreaterEqual(
                plans[p_hi].fuel_cost,
                plans[p_lo].fuel_cost - COST_TOLERANCE,
                f"fuel_cost fell from penalty={p_lo} "
                f"({plans[p_lo].fuel_cost}) to penalty={p_hi} "
                f"({plans[p_hi].fuel_cost}) beyond the summation-order "
                f"tolerance band; drawn_route={drawn_route!r}",
            )

    @given(single_leg_routes())
    @settings(deadline=None, max_examples=50)
    def test_plan_internal_consistency_across_ladder(self, drawn_route):
        """Property 6 (discretionary, direct guard on D-04 and D-07): for
        every rung with a feasible plan: objective == fuel_cost +
        penalty*stop_count exactly (no tolerance -- Decimal times int is
        exact); len(gallons) == len(stop_opis_ids); every gallons entry
        is strictly positive (D-04's nonzero-purchase rule); and
        stop_opis_ids has no duplicates and is in increasing distance
        order."""
        candidates = drawn_route[0]
        candidate_by_id = {c.opis_id: c for c in candidates}
        plans = self._plans_by_rung(drawn_route)
        if plans[Decimal(0)] is None:
            return
        for penalty, plan in plans.items():
            self.assertEqual(
                plan.objective,
                plan.fuel_cost + penalty * plan.stop_count,
                f"objective does not equal fuel_cost + penalty*stop_count "
                f"at penalty={penalty}; plan={plan!r}; drawn_route={drawn_route!r}",
            )
            self.assertEqual(
                len(plan.gallons),
                len(plan.stop_opis_ids),
                f"gallons/stop_opis_ids length mismatch at penalty={penalty}; "
                f"plan={plan!r}; drawn_route={drawn_route!r}",
            )
            for gallons in plan.gallons:
                self.assertGreater(
                    gallons,
                    Decimal(0),
                    f"a stop purchased zero gallons at penalty={penalty}; "
                    f"plan={plan!r}; drawn_route={drawn_route!r}",
                )
            self.assertEqual(
                len(set(plan.stop_opis_ids)),
                len(plan.stop_opis_ids),
                f"stop_opis_ids has duplicates at penalty={penalty}; "
                f"plan={plan!r}; drawn_route={drawn_route!r}",
            )
            distances_mi = [candidate_by_id[opis_id].distance_from_start_mi for opis_id in plan.stop_opis_ids]
            self.assertEqual(
                distances_mi,
                sorted(distances_mi),
                f"stop_opis_ids not in increasing distance order at "
                f"penalty={penalty}; plan={plan!r}; drawn_route={drawn_route!r}",
            )


class MultiLegFlattenedFixedChargeTests(SimpleTestCase):
    """D-11: a penalty-aware twin of the multi-leg flattened optimality
    class, built from offset-summed per-leg candidate lists on one
    continuous distance scale -- never a merged multi-leg line and never
    solved leg by leg. The oracle is leg-agnostic (it only ever sees one
    flattened candidate list), which is what makes this twin cheap and
    what Phase 18's PROOF-04 will build on.
    """

    @given(flattened_multi_leg_routes())
    @settings(deadline=None, max_examples=200)
    def test_penalty_zero_anchors_to_shipped_greedy_on_flattened_multi_leg(self, drawn_route):
        """The D-11 / D-09 anchor on flattened multi-leg input: same
        assertion shape as FixedChargeOracleAnchorTests above -- matching
        feasibility verdicts, and fuel cost within COST_TOLERANCE of
        solve()'s total_cost. Also asserts the drawn route really is
        multi-leg (at least two leg boundaries, flattened total equal to
        the sum of leg lengths) and that the flattened candidate count
        never exceeds MAX_STATIONS, so a strategy regression that
        silently collapses to one leg or over-generates stations fails
        loudly instead of quietly retesting the single-leg case."""
        candidates, total_route_mi, tank_range_mi, mpg, starting_fuel, leg_boundaries_mi = drawn_route

        self.assertGreaterEqual(
            len(leg_boundaries_mi),
            3,
            f"expected at least two leg boundaries beyond the start "
            f"(>=3 boundary points for >=2 legs), got "
            f"{leg_boundaries_mi!r} -- a strategy regression may have "
            f"collapsed to one leg",
        )
        self.assertEqual(
            leg_boundaries_mi[-1],
            total_route_mi,
            f"flattened total_route_mi={total_route_mi} does not equal "
            f"the sum of leg lengths (final boundary "
            f"{leg_boundaries_mi[-1]}); leg_boundaries_mi={leg_boundaries_mi!r}",
        )
        self.assertLessEqual(
            len(candidates),
            MAX_STATIONS,
            f"flattened candidate count {len(candidates)} exceeds "
            f"MAX_STATIONS={MAX_STATIONS}; candidates={candidates!r}",
        )

        try:
            greedy_plan = solve(
                candidates,
                total_route_mi,
                tank_range_mi=tank_range_mi,
                mpg=mpg,
                starting_fuel=starting_fuel,
            )
            greedy_feasible, greedy_fuel_cost = True, greedy_plan.total_cost
        except InfeasibleRouteError:
            greedy_feasible, greedy_fuel_cost = False, None

        oracle_plan = optimal_fixed_charge_plan(
            candidates,
            total_route_mi,
            penalty=Decimal(0),
            tank_range_mi=tank_range_mi,
            mpg=mpg,
            starting_fuel=starting_fuel,
        )
        oracle_feasible = oracle_plan is not None

        context = (
            f"candidates={candidates!r}, total_route_mi={total_route_mi}, "
            f"leg_boundaries_mi={leg_boundaries_mi!r}, "
            f"tank_range_mi={tank_range_mi}, mpg={mpg}, starting_fuel={starting_fuel}"
        )
        self.assertEqual(
            greedy_feasible,
            oracle_feasible,
            f"feasibility verdicts disagree on flattened multi-leg "
            f"input: greedy={greedy_feasible}, oracle={oracle_feasible}; {context}",
        )
        if greedy_feasible:
            self.assertLessEqual(
                abs(oracle_plan.fuel_cost - greedy_fuel_cost),
                COST_TOLERANCE,
                f"oracle fuel_cost={oracle_plan.fuel_cost} vs greedy "
                f"total_cost={greedy_fuel_cost} exceeds tolerance on "
                f"flattened multi-leg input; {context}",
            )

    @given(flattened_multi_leg_routes())
    @settings(deadline=None, max_examples=50)
    def test_penalty_aware_invariant_on_flattened_multi_leg(self, drawn_route):
        """What makes this twin penalty-aware rather than a second copy
        of the pure-fuel proof: at DEFAULT_PENALTY versus Decimal(0) on
        the same flattened multi-leg route, stop count is non-increasing
        and objective is non-decreasing -- the same D-10 shape as
        FixedChargePenaltyConsistencyTests, exercised on flattened
        multi-leg input."""
        candidates, total_route_mi, tank_range_mi, mpg, starting_fuel, leg_boundaries_mi = drawn_route

        zero_plan = optimal_fixed_charge_plan(
            candidates,
            total_route_mi,
            penalty=Decimal(0),
            tank_range_mi=tank_range_mi,
            mpg=mpg,
            starting_fuel=starting_fuel,
        )
        penalty_plan = optimal_fixed_charge_plan(
            candidates,
            total_route_mi,
            penalty=DEFAULT_PENALTY,
            tank_range_mi=tank_range_mi,
            mpg=mpg,
            starting_fuel=starting_fuel,
        )

        context = (
            f"candidates={candidates!r}, total_route_mi={total_route_mi}, "
            f"leg_boundaries_mi={leg_boundaries_mi!r}"
        )
        self.assertEqual(
            zero_plan is None,
            penalty_plan is None,
            f"feasibility at penalty={DEFAULT_PENALTY} disagrees with "
            f"the penalty=0 verdict on flattened multi-leg input; {context}",
        )
        if zero_plan is None:
            return
        self.assertLessEqual(
            penalty_plan.stop_count,
            zero_plan.stop_count,
            f"stop_count rose from penalty=0 ({zero_plan.stop_count}) to "
            f"penalty={DEFAULT_PENALTY} ({penalty_plan.stop_count}) on "
            f"flattened multi-leg input; {context}",
        )
        self.assertGreaterEqual(
            penalty_plan.objective,
            zero_plan.objective,
            f"objective fell from penalty=0 ({zero_plan.objective}) to "
            f"penalty={DEFAULT_PENALTY} ({penalty_plan.objective}) on "
            f"flattened multi-leg input; {context}",
        )
