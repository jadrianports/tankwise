"""Property-based proof that the solver's greedy is a true cost optimum.

Compares `routing.services.solver.solve()` against an independent,
deliberately dumb, exhaustive oracle across randomized price/position
landscapes. The oracle lives here only -- it is a memoized recursive
search over `(node, fuel-remaining)` states, and it never imports or
calls any solver helper beyond the `Candidate` dataclass, so a passing
test is evidence about the solver's actual behavior, not about
assumptions shared between the two implementations.

Plain `django.test.SimpleTestCase` + `@given` -- not
`hypothesis.extra.django.TestCase`. The solver is a pure function with
no ORM/DB access, so the Django Hypothesis integration would only add a
per-example database transaction for nothing.
"""
from decimal import Decimal

from django.test import SimpleTestCase
from hypothesis import given, settings, strategies as st

from routing.services import Candidate, solve
from routing.services.exceptions import InfeasibleRouteError

# Bounds the exhaustive oracle's state space: with this many stations the
# (node, fuel) memoization table stays tiny and 200 examples run in well
# under a second combined.
MAX_STATIONS = 6


def _useful_purchase_amounts(node_index, pos, fuel, ordered, total_route_mi, tank_range_mi):
    """Enumerate the finite set of purchase amounts that could possibly be
    part of an optimal plan from a purchasable node.

    Buying fuel that never gets burned strictly increases cost, so an
    optimal purchase either brings the tank to exactly the amount needed
    to reach some later node (a station further down the route, or
    FINISH), or fills the tank to capacity. No other amount can ever be
    part of an optimal plan: anything strictly between two of these
    values spends money on reach the plan doesn't use, and anything past
    "fill to capacity" cannot physically fit in the tank. Enumerating
    this finite set is therefore exhaustive over optima while keeping the
    search small.
    """
    targets = [c.distance_from_start_mi for c in ordered[node_index + 1 :]]
    targets.append(total_route_mi)

    amounts = set()
    for target_dist in targets:
        needed = target_dist - pos
        if needed <= 0 or needed > tank_range_mi:
            # Unreachable from this node in a single hop no matter how
            # much is bought here -- not a useful purchase amount.
            continue
        amounts.add(max(Decimal(0), needed - fuel))

    fill = tank_range_mi - fuel
    if fill >= 0:
        amounts.add(fill)

    if not amounts:
        amounts.add(Decimal(0))

    return amounts


def _brute_force_optimum(candidates, total_route_mi, tank_range_mi, mpg, starting_fuel):
    """Deliberately dumb exhaustive search for the minimum-cost feasible
    fueling plan. Test-only -- never imported by `routing.services`.

    Models the trip as a memoized search over
    `(node_index, fuel_miles_remaining)` states, where `node_index == -1`
    is the non-purchasable START node and `node_index >= 0` indexes into
    `ordered` (stations sorted by position, matching the solver's own
    ordering). From each state it enumerates every useful purchase amount
    (see `_useful_purchase_amounts`), then every reachable next node --
    any later station, or FINISH directly -- and recurses, taking the
    minimum total cost across every choice. It makes no assumption about
    which stations belong in the optimal stop set; subsets fall out of
    the search rather than being reasoned about. No pruning heuristics,
    no price-based shortcuts, no reuse of any solver helper beyond the
    `Candidate` dataclass.

    Returns the minimum total cost as a Decimal, or None if no feasible
    plan exists.
    """
    ordered = sorted(
        candidates,
        key=lambda c: (c.distance_from_start_mi, c.price_per_gallon, c.opis_id),
    )
    node_count = len(ordered)
    memo = {}

    def position_of(node_index):
        if node_index == -1:
            return Decimal(0)
        return ordered[node_index].distance_from_start_mi

    def best_cost_from(node_index, fuel):
        key = (node_index, fuel)
        if key in memo:
            return memo[key]

        pos = position_of(node_index)
        purchasable = node_index != -1

        if purchasable:
            price = ordered[node_index].price_per_gallon
            amounts = _useful_purchase_amounts(
                node_index, pos, fuel, ordered, total_route_mi, tank_range_mi
            )
        else:
            # START is non-purchasable -- fuel here is fixed at whatever
            # the vehicle started the trip with, never topped off.
            amounts = {Decimal(0)}

        best = None
        for amount in amounts:
            new_fuel = fuel + amount
            if new_fuel > tank_range_mi:
                continue
            cost_here = (amount / mpg) * price if purchasable and amount > 0 else Decimal(0)

            dist_to_finish = total_route_mi - pos
            if dist_to_finish <= new_fuel:
                if best is None or cost_here < best:
                    best = cost_here

            for j in range(node_index + 1, node_count):
                gap = ordered[j].distance_from_start_mi - pos
                if gap <= 0 or gap > new_fuel:
                    continue
                sub_cost = best_cost_from(j, new_fuel - gap)
                if sub_cost is None:
                    continue
                total = cost_here + sub_cost
                if best is None or total < best:
                    best = total

        memo[key] = best
        return best

    return best_cost_from(-1, starting_fuel * tank_range_mi)


class SolverOptimalityTests(SimpleTestCase):
    """The solver's output must match an independent exhaustive
    oracle's optimum across randomized price/position landscapes,
    including partial starting tanks -- the exact case the START-node fix
    exists to protect."""

    @given(
        stations=st.lists(
            st.tuples(
                st.decimals(min_value="1.00", max_value="6.00", places=2),
                st.decimals(min_value="1", max_value="800", places=0),
            ),
            min_size=0,
            max_size=MAX_STATIONS,
            unique_by=lambda t: t[1],
        ),
        tank_range_mi=st.decimals(min_value="20", max_value="800", places=0),
        mpg=st.decimals(min_value="1", max_value="50", places=0),
        starting_fuel=st.decimals(min_value="0.00", max_value="1.00", places=2),
        total_route_mi=st.decimals(min_value="1", max_value="800", places=0),
    )
    @settings(deadline=None, max_examples=200)
    def test_solver_matches_brute_force_optimum(
        self, stations, tank_range_mi, mpg, starting_fuel, total_route_mi
    ):
        # The solver rejects out-of-range candidates with
        # InvalidRouteInputError -- that's input validation, not the
        # optimality property under test, so filter here rather than
        # generate around it.
        candidates = [
            Candidate(
                name=f"S{i}",
                opis_id=i,
                price_per_gallon=price,
                distance_from_start_mi=dist,
            )
            for i, (price, dist) in enumerate(stations)
            if dist <= total_route_mi
        ]

        try:
            plan = solve(
                candidates,
                total_route_mi,
                tank_range_mi=tank_range_mi,
                mpg=mpg,
                starting_fuel=starting_fuel,
            )
        except InfeasibleRouteError:
            solver_feasible, solver_cost = False, None
        else:
            solver_feasible, solver_cost = True, plan.total_cost

        oracle_cost = _brute_force_optimum(
            candidates, total_route_mi, tank_range_mi, mpg, starting_fuel
        )

        self.assertEqual(solver_feasible, oracle_cost is not None)
        if solver_feasible:
            # Both the solver and the oracle accumulate purchases in a
            # different order, and Decimal division at the default
            # 28-digit context is inexact, so bit-identical totals are
            # not guaranteed even when both are genuinely optimal. This
            # tolerance is four orders of magnitude tighter than a cent,
            # so it cannot mask a real optimality bug -- only Decimal
            # rounding noise from summation order.
            self.assertLessEqual(abs(solver_cost - oracle_cost), Decimal("0.0001"))
