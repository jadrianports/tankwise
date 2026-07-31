"""Fixed-charge fuel-stop dynamic program: fuel dollars plus a flat per-stop
penalty, minimised jointly over which stations to buy at and how much to
buy at each.

Request-path math only -- no Django, no DB, no HTTP client. All money and
gallon values are exact, unrounded `Decimal`; rounding to cents happens
only at the HTTP response serialization boundary, exactly as in
`solver.py`'s own purity header.

Nothing in production calls this module yet (D-34). It lives inside the
AST-gated solver boundary (see `SOLVER_FILES` in
`routing/tests/test_boundaries.py`) alongside `solver.py`, `exceptions.py`
and `prune.py`. `solve()` still runs the pre-Phase-18 greedy; a later plan
wires this module in as `solve()`'s delegate.

## The finite-fill lemma

At any purchasable station, given every remaining candidate strictly
ahead of it (on the route's distance axis) plus FINISH as the possible
next-purchase points, the only post-purchase fuel levels that can ever be
part of an optimal plan are: the level already on board (buy nothing),
the level that lands exactly on one of those remaining candidates or
FINISH, and the level that fills the tank to capacity. Buying anything
strictly between two of those values spends money on range the plan never
uses; buying past "fill to capacity" cannot physically fit in the tank.

Proof (perturbation argument): take any optimal assignment and suppose
some station's purchase amount is neither an exact-reach amount nor a
fill-to-capacity amount. Then there is slack in both directions, so
perturb by a small positive epsilon. If this station's price is strictly
below the price at the next purchase point, buy epsilon more here and
epsilon less there -- possible because the tank is not yet full here and
there is slack to give up there -- and total fuel cost strictly falls,
contradicting optimality. If the price here is strictly above the next
purchase point's price, buy epsilon less here and epsilon more there --
possible because we are above the exact-reach amount here -- and total
cost strictly falls, again a contradiction. If the two prices are equal,
the perturbation is cost-neutral, so an optimum also exists at one of the
interval's endpoints. Either way, an optimum exists at one of the finitely
many listed amounts. The penalty term never changes this argument: a stop
count is fixed by which purchases are strictly positive, and the
perturbation above never drives a strictly positive purchase to exactly
zero (it moves fuel between two purchases that are both already positive
by assumption), so the fixed-charge term is unaffected by the argument and
enters the objective independently of it.

This is the **third independent derivation** of this lemma in the
codebase: `_useful_purchase_amounts` (`test_solver_optimality.py:30`)
derives it for the pure-fuel objective over a memoized `(node, fuel)`
oracle search; `_useful_fill_levels_mi`
(`test_solver_fixed_charge_optimality.py:244`) derives it for the
fixed-charge objective, but scoped to one fixed subset's own remaining
members (the subset-enumeration oracle pre-selects the whole station set
before computing any purchase amount, so it never needs to consider
candidates outside that subset). This module's version is scoped
differently again: it ranges over **every** remaining sorted candidate
plus FINISH, because this module does not pre-select a subset -- every
candidate ahead is a live possibility at every state, so the finiteness
argument must hold over all of them at once, not one subset's own
members. Neither prior derivation is imported, ported, or transcribed
here.

Bound: the number of distinct fuel levels reachable at any node is at
most (candidates strictly ahead of that node) + 1 -- one level per
remaining candidate/FINISH exact-reach amount, plus the fill-to-capacity
amount, deduplicated. That bounds the DP's fuel dimension and gives an
`O(m^2)` state count over `m` post-prune candidates.

## Totality

`preflight_gap_check` passing **is** the feasibility condition: if every
consecutive gap (STARTing at START, ending at FINISH, sorted by position)
fits within the usable range at that node, then stopping at every station
in turn is itself a feasible plan. Consequently the recurrence in
`solve_fixed_charge` is total -- it never raises, and infeasibility has
exactly one source in the codebase: `preflight_gap_check`, run by the
caller before the recurrence is ever invoked (D-17).
"""
from decimal import Decimal

from routing.services.exceptions import InfeasibleRouteError


def _as_decimal(value):
    return value if isinstance(value, Decimal) else Decimal(str(value))


def preflight_gap_check(candidates, *, total_route_mi, tank_range_mi, starting_fuel):
    """Raise `InfeasibleRouteError` if any consecutive gap between
    along-route nodes (START, each candidate in position order, FINISH)
    exceeds the usable range at the node the gap starts from. Return
    `None` when every gap fits.

    Price-independent by design: reads only `distance_from_start_mi` and
    `name` from each candidate, never `price_per_gallon` -- this check
    answers a pure reachability question, not a cost one.

    START is a non-purchasable node: the usable range there is the fuel
    actually on board (`starting_fuel * tank_range_mi`), never the tank's
    full capacity -- the **START asymmetry** (D-17, `solver.py:173`). At
    every real station the tank can be topped off, so the usable range
    there is `tank_range_mi`.

    Never passes `leg_index` or `leg_coords` to the raised exception --
    `views.py::_enrich_infeasible_leg` is their sole setter, outside this
    AST-import-gated module.
    """
    total_route_mi = _as_decimal(total_route_mi)
    tank_range_mi = _as_decimal(tank_range_mi)
    starting_fuel = _as_decimal(starting_fuel)

    ordered = sorted(candidates, key=lambda c: (c.distance_from_start_mi, c.opis_id))

    pos = Decimal(0)
    current_name = "START"
    usable_range = starting_fuel * tank_range_mi

    for candidate in ordered:
        gap = candidate.distance_from_start_mi - pos
        if gap > usable_range:
            raise InfeasibleRouteError(
                from_station=current_name,
                to_station=candidate.name,
                gap_mi=gap,
                max_range_mi=usable_range,
            )
        pos = candidate.distance_from_start_mi
        current_name = candidate.name
        usable_range = tank_range_mi

    gap = total_route_mi - pos
    if gap > usable_range:
        raise InfeasibleRouteError(
            from_station=current_name,
            to_station="FINISH",
            gap_mi=gap,
            max_range_mi=usable_range,
        )


def useful_fill_levels_mi(
    position_mi, fuel_on_arrival_mi, *, tank_range_mi, nodes_ahead_mi
):
    """Return the strictly increasing, deduplicated tuple of useful
    post-purchase fuel levels (in miles of range) at a station positioned
    at `position_mi`, given the fuel already on board on arrival and the
    distances of every remaining candidate strictly ahead plus FINISH
    (`nodes_ahead_mi`) -- not one fixed subset's own remaining members.
    See the module docstring's "The finite-fill lemma" for the proof this
    set is exhaustive over optima.

    The returned levels are: the arrival level itself (buy nothing), each
    level that exactly reaches a node in `nodes_ahead_mi` that lies within
    `tank_range_mi` of `position_mi`, and `tank_range_mi` (fill the tank)
    -- clamped to `[fuel_on_arrival_mi, tank_range_mi]`. Levels below the
    arrival level are impossible (fuel cannot be sold) and are excluded.
    """
    levels = {fuel_on_arrival_mi, tank_range_mi}
    for node_mi in nodes_ahead_mi:
        reach_mi = node_mi - position_mi
        if reach_mi <= tank_range_mi:
            levels.add(reach_mi)
    return tuple(
        sorted(
            level
            for level in levels
            if fuel_on_arrival_mi <= level <= tank_range_mi
        )
    )
