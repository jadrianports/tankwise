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

## Determinism

No `set` or `dict` iteration order may influence the recurrence's
outcome. Candidates are sorted once, defensively, by the same total order
`prune.py` documents (`distance_from_start_mi`, `price_per_gallon`,
`opis_id`); DP nodes are keyed by that sorted list's ordinal index, never
by raw mile value (two co-located candidates therefore always stay
distinct nodes). Every relaxation "is this better?" test and the final
winner selection resolve through the same explicit D-12 key -- there is
no last-writer-wins path. `dict` tables in this module are only ever
populated by, and iterated in, an order fully determined by that sorted
input and that key, so re-running `solve_fixed_charge` on the same input
always retraces the same relaxations in the same order and produces a
byte-identical `FuelPlan`.

## Trivial-stop bound

A stop survives the optimum only when the gallons bought there, times the
price advantage of routing through it, exceeds the penalty charged for
making the stop -- `gallons >= penalty / price_advantage` falls
structurally out of the objective, not out of luck: any purchase smaller
than that break-even point costs more in penalty than it saves in fuel,
so the DP's own cost comparison discards it in favor of not stopping (or
stopping less). At the UI default (6.5 mpg, 1,050 mi tank, $35 penalty), a
purchase under 10% of tank capacity (about 68 gallons) would need a price
advantage above roughly $2/gal to break even -- far outside anything in
the dataset, which is why trivial stops are structurally rare under this
objective rather than merely uncommon.

A hard minimum-gallons floor inside the solver was **considered and
rejected**: `REQUIREMENTS.md`'s Out of Scope table rejects it by name
because it can render a sparse corridor infeasible when no station ahead
can supply the floor amount, and it would break the optimality proof this
milestone is built on -- the finite-fill lemma above assumes every useful
purchase amount stays reachable, not artificially excluded.
"""
from dataclasses import dataclass
from decimal import Decimal

from routing.services.exceptions import InfeasibleRouteError
from routing.services.solver import FuelPlan, FuelStop, PurchaseReason

# D-12's tolerance band for the DP's own relaxation and winner-selection
# comparisons: two objectives within this band are treated as tied and
# the comparison falls through to the next tie-break key (fewest stops,
# then station positions, then opis_id) rather than one winning purely on
# Decimal summation-order noise. Never used as comparison slop beyond
# that -- byte-identical to the fixed-charge oracle's own COST_TOLERANCE
# (`test_solver_fixed_charge_optimality.py:96`).
COST_TOLERANCE = Decimal("0.0001")


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


@dataclass
class _EdgeInfo:
    """The rationale recorded on one relaxation edge, at the moment the
    purchase it represents was chosen -- never re-derived later by
    inspecting the finished plan (D-03)."""

    name: str
    opis_id: int
    price: Decimal
    pos: Decimal
    gallons: Decimal
    cost: Decimal
    reason: str
    reason_target_opis_id: int | None
    reason_target_name: str | None
    bypassed_cheaper_count: int
    bypassed_saving_forgone: Decimal | None


@dataclass
class _StateRecord:
    """One `(node, fuel)` DP state: the winning D-12 key that reached it,
    the predecessor `(node_index, fuel_level)` to continue the backward
    walk from, and the edge that produced it (`None` for a "buy nothing"
    pass-through or for the initial START state)."""

    key: tuple
    predecessor: tuple | None
    edge: "_EdgeInfo | None"


def _key_less(key_a, key_b):
    """The D-12 total order: lowest objective (within `COST_TOLERANCE`,
    for summation-order noise only -- never comparison slop), then fewest
    stops, then the sorted tuple of chosen stations' positions, then the
    tuple of their `opis_id`. Byte-identical to the fixed-charge oracle's
    tie-break (`optimal_fixed_charge_plan`,
    `test_solver_fixed_charge_optimality.py:391`)."""
    objective_a, stops_a, positions_a, opis_a = key_a
    objective_b, stops_b, positions_b, opis_b = key_b
    if abs(objective_a - objective_b) > COST_TOLERANCE:
        return objective_a < objective_b
    if stops_a != stops_b:
        return stops_a < stops_b
    if positions_a != positions_b:
        return positions_a < positions_b
    return opis_a < opis_b


def solve_fixed_charge(
    candidates, *, total_route_mi, tank_range_mi, mpg, starting_fuel, penalty
) -> FuelPlan:
    """Return the `FuelPlan` minimising fuel dollars plus `penalty` times
    the count of stations bought at strictly more than zero, over a
    `(node, fuel)` state space with no hard stop cap (SOLV-01, SOLV-02).

    Assumes `preflight_gap_check` has already passed over this same
    `candidates`/`total_route_mi`/`tank_range_mi`/`starting_fuel` --
    the recurrence never raises (see "Totality" above) and is only total
    under that precondition.

    `candidates` is defensively re-sorted here by the total order
    `prune.py` already documents; DP nodes are the sorted list's ordinal
    positions plus a terminal FINISH node, never the raw
    `distance_from_start_mi` value, so co-located candidates always stay
    distinct nodes.

    From each `(node, fuel_on_arrival)` state, `useful_fill_levels_mi`
    enumerates every useful post-purchase fuel level. For each level, the
    vehicle travels to the farthest node (candidate or FINISH) reachable
    at that level -- any closer node is already reachable via its own,
    smaller, exact-reach level from the same state, so passing a closer
    node without stopping there needs no separate state (a "buy nothing"
    continuation from that node would be a no-op with identical cost and
    stop count). When more than one node shares that farthest position
    (a co-located pair), every one of them becomes a separate target
    state -- none is silently dropped.

    Every relaxation records, alongside its predecessor pointer, the
    `purchase_reason` for the purchase it represents (derived by the same
    price-ahead tests the greedy uses, so the reasons agree with the
    frozen greedy at `penalty=0`) plus `bypassed_cheaper_count` and
    `bypassed_saving_forgone` -- how many strictly-cheaper stations
    reachable within one full tank from this node were evaluated and not
    routed through, and the fuel-dollar saving that gave up.
    `BYPASS_CHEAPER_NOT_WORTH_STOP` is the reason exactly when a full-tank
    fill's farthest target is not one of those reachable cheaper stations
    -- structurally unreachable as a *winning* edge at `penalty=0`,
    because the standard fuel-cost exchange argument always prefers
    buying only enough to reach a strictly cheaper station over
    overpaying at the current one, until a nonzero penalty makes the
    extra stop's cost outweigh the saving.

    Reconstruction walks predecessor pointers from the winning FINISH
    state back to START, emitting one `FuelStop` per strictly-positive
    purchase in increasing `distance_from_start_mi` order.
    `skipped_count`, `skipped_avg_price`, `price_percentile` and
    `corridor_avg_price` are left at their defaults -- those are computed
    over the full, unpruned candidate list by `solve()` itself (D-20),
    never by this module. `total_cost` never includes the penalty
    (INTG-02); `penalised_objective` is `total_cost + penalty * len(stops)`,
    computed fresh from the reconstructed stops rather than reused from
    internal DP bookkeeping.
    """
    total_route_mi = _as_decimal(total_route_mi)
    tank_range_mi = _as_decimal(tank_range_mi)
    mpg = _as_decimal(mpg)
    starting_fuel = _as_decimal(starting_fuel)
    penalty = _as_decimal(penalty)

    ordered = sorted(
        candidates,
        key=lambda c: (c.distance_from_start_mi, c.price_per_gallon, c.opis_id),
    )
    node_count = len(ordered)
    positions = [c.distance_from_start_mi for c in ordered]
    finish = node_count  # terminal node index, one past the last candidate

    def nodes_ahead_of(node_index):
        ahead = [(positions[j], j) for j in range(node_index + 1, node_count)]
        ahead.append((total_route_mi, finish))
        return ahead

    def farthest_reachable(ahead, pos, level):
        max_pos = pos + level
        in_range = [entry for entry in ahead if entry[0] <= max_pos]
        if not in_range:
            return []
        farthest_pos = in_range[-1][0]
        return [entry for entry in in_range if entry[0] == farthest_pos]

    states = {i: {} for i in range(-1, node_count + 1)}
    start_key = (Decimal(0), 0, (), ())
    start_fuel = starting_fuel * tank_range_mi
    states[-1][start_fuel] = _StateRecord(key=start_key, predecessor=None, edge=None)

    def relax(target_index, arrival_fuel, new_key, predecessor, edge):
        existing = states[target_index].get(arrival_fuel)
        if existing is None or _key_less(new_key, existing.key):
            states[target_index][arrival_fuel] = _StateRecord(
                key=new_key, predecessor=predecessor, edge=edge
            )

    # START is non-purchasable: exactly one fixed departure level.
    start_ahead = nodes_ahead_of(-1)
    for target_pos, target_index in farthest_reachable(
        start_ahead, Decimal(0), start_fuel
    ):
        relax(
            target_index,
            start_fuel - target_pos,
            start_key,
            (-1, start_fuel),
            None,
        )

    for node_index in range(node_count):
        station = ordered[node_index]
        pos = positions[node_index]
        ahead = nodes_ahead_of(node_index)

        reachable_cheaper = [
            (p, idx)
            for (p, idx) in ahead
            if idx != finish
            and p - pos <= tank_range_mi
            and ordered[idx].price_per_gallon < station.price_per_gallon
        ]
        ahead_has_cheaper = any(
            idx != finish and ordered[idx].price_per_gallon < station.price_per_gallon
            for (p, idx) in ahead
        )

        for fuel_on_arrival, record in list(states[node_index].items()):
            levels = useful_fill_levels_mi(
                pos,
                fuel_on_arrival,
                tank_range_mi=tank_range_mi,
                nodes_ahead_mi=[p for (p, idx) in ahead],
            )
            for level in levels:
                targets = farthest_reachable(ahead, pos, level)
                if not targets:
                    continue

                buy_mi = level - fuel_on_arrival
                is_purchase = buy_mi > 0

                if is_purchase:
                    gallons = buy_mi / mpg
                    cost = gallons * station.price_per_gallon
                    target_index = targets[0][1]
                    is_full_fill = level == tank_range_mi
                    bypassed_count = 0
                    bypassed_saving = None

                    if target_index == finish:
                        reason = PurchaseReason.REACH_FINISH
                    elif not is_full_fill:
                        reason = PurchaseReason.REACH_CHEAPER_STOP
                    elif reachable_cheaper:
                        reason = PurchaseReason.BYPASS_CHEAPER_NOT_WORTH_STOP
                        bypassed_count = len(reachable_cheaper)
                        saving_total = Decimal(0)
                        for cheaper_pos, cheaper_idx in reachable_cheaper:
                            cheaper_gap = cheaper_pos - pos
                            cheaper_buy_mi = max(
                                Decimal(0), cheaper_gap - fuel_on_arrival
                            )
                            cheaper_gallons = cheaper_buy_mi / mpg
                            saving_total += cheaper_gallons * (
                                station.price_per_gallon
                                - ordered[cheaper_idx].price_per_gallon
                            )
                        bypassed_saving = saving_total
                    elif ahead_has_cheaper:
                        reason = PurchaseReason.FILL_TO_CONTINUE
                    else:
                        reason = PurchaseReason.TOP_UP_AT_CHEAPEST

                    reason_target_opis_id = (
                        ordered[target_index].opis_id
                        if target_index != finish
                        else None
                    )
                    reason_target_name = (
                        ordered[target_index].name if target_index != finish else None
                    )

                    new_key = (
                        record.key[0] + cost + penalty,
                        record.key[1] + 1,
                        record.key[2] + (pos,),
                        record.key[3] + (station.opis_id,),
                    )
                    edge = _EdgeInfo(
                        name=station.name,
                        opis_id=station.opis_id,
                        price=station.price_per_gallon,
                        pos=pos,
                        gallons=gallons,
                        cost=cost,
                        reason=reason,
                        reason_target_opis_id=reason_target_opis_id,
                        reason_target_name=reason_target_name,
                        bypassed_cheaper_count=bypassed_count,
                        bypassed_saving_forgone=bypassed_saving,
                    )
                else:
                    new_key = record.key
                    edge = None

                predecessor = (node_index, fuel_on_arrival)
                for target_pos, target_index in targets:
                    relax(
                        target_index,
                        level - (target_pos - pos),
                        new_key,
                        predecessor,
                        edge,
                    )

    winner = None
    for record in states[finish].values():
        if winner is None or _key_less(record.key, winner.key):
            winner = record

    if winner is None:
        # Only reachable when a caller skips the documented
        # `preflight_gap_check` precondition on genuinely infeasible
        # input -- surfaced as a clear, named failure rather than a bare
        # AttributeError/KeyError from an empty FINISH state table.
        raise InfeasibleRouteError(
            from_station="START" if node_count == 0 else ordered[-1].name,
            to_station="FINISH",
            gap_mi=total_route_mi - (Decimal(0) if node_count == 0 else positions[-1]),
            max_range_mi=tank_range_mi,
        )

    edges = []
    current = winner
    while current.predecessor is not None:
        if current.edge is not None:
            edges.append(current.edge)
        predecessor_index, predecessor_fuel = current.predecessor
        current = states[predecessor_index][predecessor_fuel]
    edges.reverse()

    stops = [
        FuelStop(
            name=edge.name,
            opis_id=edge.opis_id,
            price_per_gallon=edge.price,
            distance_from_start_mi=edge.pos,
            gallons=edge.gallons,
            cost=edge.cost,
            purchase_reason=edge.reason,
            reason_target_opis_id=edge.reason_target_opis_id,
            reason_target_name=edge.reason_target_name,
            bypassed_cheaper_count=edge.bypassed_cheaper_count,
            bypassed_saving_forgone=edge.bypassed_saving_forgone,
        )
        for edge in edges
    ]

    total_cost = sum((s.cost for s in stops), Decimal(0))
    total_gallons = sum((s.gallons for s in stops), Decimal(0))

    return FuelPlan(
        stops=stops,
        total_cost=total_cost,
        total_gallons=total_gallons,
        penalised_objective=total_cost + penalty * len(stops),
        penalty_applied=penalty,
    )
