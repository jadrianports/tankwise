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
