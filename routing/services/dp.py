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
import bisect
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
    reachable within one full tank from this node were evaluated, sit
    strictly before THIS edge's own target, and were not routed through,
    and the fuel-dollar saving that gave up. A reachable-cheaper station
    that this edge's own target coincides with was not bypassed -- it was
    reached -- so it is excluded from that count (a full-tank fill's exact
    reach amount can coincide with a cheaper station's position, and
    landing there is `REACH_CHEAPER_STOP`, never a bypass of itself).
    `BYPASS_CHEAPER_NOT_WORTH_STOP` is the reason exactly when a full-tank
    fill flies past at least one such station (strictly before its own
    target) AND the flat per-stop `penalty` strictly outweighs the summed
    fuel-dollar saving those stations would have offered -- both
    conditions together, never bypass-count alone. Because that saving
    total is never negative and `penalty` is never negative, the
    penalty-outweighs-saving test can never hold at `penalty=0`, so this
    reason is structurally unreachable as a *winning* edge there -- the
    standard fuel-cost exchange argument always prefers buying only enough
    to reach a strictly cheaper station over overpaying at the current
    one, until a nonzero penalty makes the extra stop's cost outweigh the
    saving.

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

    ## Implementation note: the fuel/position domain runs on exact integer
    ## ticks, never on `Decimal` arithmetic, in the O(states x levels x
    ## targets) inner loop

    Every quantity in the *position* domain -- station positions,
    `tank_range_mi`, `total_route_mi`, `starting_fuel * tank_range_mi`, and
    every fuel level `useful_fill_levels_mi` can ever return -- is a sum or
    difference of a small, fixed set of input `Decimal`s. This function
    picks one common exponent (`_pos_exponent`, the finest -- most
    negative -- exponent already present among those inputs) once, up
    front, and re-expresses every position-domain value at that exponent as
    a plain Python `int` ("ticks"): `_to_ticks`/`_from_ticks` convert via
    `Decimal.as_tuple()`'s exact `(sign, digits, exponent)` triple, never
    via `Decimal` division or `scaleb` (both of which are, in general,
    subject to context-precision rounding) -- so the conversion is a exact
    change of representation, not an approximation. Addition and
    subtraction are closed under a fixed exponent (the sum/difference of
    two values exactly representable at exponent E is itself exactly
    representable at exponent E), so every arrival-fuel level, every
    reachable-target position, and every DP state key computed by walking
    that arithmetic in `int` space is bit-for-bit the same value the
    original all-`Decimal` recurrence would have produced -- just computed
    without `Decimal`'s per-operation object overhead, and with the
    (already position-sorted) reachable-target scan replaced by
    `bisect.bisect_right` instead of a linear filter. Nothing in the
    *money* domain (`gallons`, `cost`, the objective, the D-12 tie-break)
    is touched by this -- `buy_mi = level - fuel_on_arrival` is still
    computed from the original `Decimal` `level` (`useful_fill_levels_mi`'s
    own unmodified return value) and the original `Decimal`
    `fuel_on_arrival` reconstructed via `_from_ticks`, so `gallons = buy_mi
    / mpg` and everything downstream of it runs through the exact same
    `Decimal` division/multiplication/comparison sequence, in the exact
    same order, as before this optimization existed.
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

    def reachable_targets(ahead, pos, level):
        """Every node in `ahead` within `level` miles of `pos`, in
        increasing-position order -- not merely the farthest one. A node
        strictly closer than the farthest reachable one is a genuinely
        distinct, non-dominated (node, fuel_on_arrival) state: it carries
        MORE leftover fuel than any smaller, exact-reach-to-that-node
        level would (which arrives with zero fuel to spare), at the cost
        of having paid for that extra range at the CURRENT station's
        price rather than a possibly cheaper later one. Collapsing to only
        the farthest node -- as an earlier version of this function did --
        silently discards that state, which can be strictly optimal (e.g.
        filling to capacity at the cheapest station on the route, then
        topping off only the small remainder at a pricier one, rather than
        buying that remainder's full distance at the pricier price).

        Retained for START's own one-time reachability computation below
        (never on the O(states x levels) hot path -- see `_reachable_ticks`
        for that)."""
        max_pos = pos + level
        return [entry for entry in ahead if entry[0] <= max_pos]

    # --- Exact integer-tick domain for positions/fuel levels (see the
    # "Implementation note" in this function's docstring). Chosen once,
    # covering every position-domain Decimal this run can ever produce:
    # candidate positions, tank_range_mi, total_route_mi, and
    # starting_fuel * tank_range_mi (the one position-domain multiplication
    # in this function -- every other position-domain value is a sum or
    # difference of these, so its own exponent is never finer than
    # min() of the four categories below).
    start_fuel = starting_fuel * tank_range_mi

    _pos_exponent = min(
        value.as_tuple().exponent
        for value in (total_route_mi, tank_range_mi, start_fuel, *positions)
    )

    def _to_ticks(value):
        sign, digits, exponent = value.as_tuple()
        coefficient = 0
        for digit in digits:
            coefficient = coefficient * 10 + digit
        if sign:
            coefficient = -coefficient
        shift = exponent - _pos_exponent
        if shift < 0:
            # Unreachable given _pos_exponent is the min over every
            # position-domain value this function ever converts (see
            # docstring) -- surfaced loudly rather than silently truncated
            # if that invariant is ever broken by a future edit.
            raise AssertionError(
                f"position-domain value {value!r} needs more precision "
                f"than the derived tick scale (exponent {exponent} < "
                f"{_pos_exponent})"
            )
        return coefficient * (10**shift) if shift else coefficient

    def _from_ticks(ticks):
        sign = 1 if ticks < 0 else 0
        magnitude = -ticks if sign else ticks
        digit_str = str(magnitude) if magnitude else "0"
        return Decimal((sign, tuple(int(d) for d in digit_str), _pos_exponent))

    positions_ticks = [_to_ticks(p) for p in positions]
    total_route_ticks = _to_ticks(total_route_mi)
    start_fuel_ticks = _to_ticks(start_fuel)
    # full_ticks[i] is node i's position in ticks, for i in [0, node_count]
    # -- station nodes at their own index, FINISH (index node_count) at
    # total_route_ticks. Ascending by construction: positions is already
    # sorted, and `solver._validate` guarantees every candidate's
    # distance_from_start_mi <= total_route_mi, so total_route_ticks is
    # always >= the last station tick.
    full_ticks = positions_ticks + [total_route_ticks]

    def _reachable_ticks(node_index, max_pos_ticks):
        """Every node index in (node_index, node_count] (stations, then
        FINISH) whose tick position is <= max_pos_ticks, as a `range` in
        increasing-position order -- the tick-domain, O(log n) equivalent
        of `reachable_targets` above (binary search over the already
        position-sorted `full_ticks`, replacing its O(n) linear scan)."""
        lo = node_index + 1
        hi_bound = node_count + 1
        hi = bisect.bisect_right(full_ticks, max_pos_ticks, lo, hi_bound)
        return range(lo, hi)

    states = {i: {} for i in range(-1, node_count + 1)}
    start_key = (Decimal(0), 0, (), ())
    states[-1][start_fuel_ticks] = _StateRecord(
        key=start_key, predecessor=None, edge=None
    )

    def _wins(target_index, arrival_fuel_ticks, new_key):
        """Cheap winner-check only -- no state mutation, no `_EdgeInfo`
        construction. Split out from `relax` (below) so the *expensive*
        per-target purchase-reason computation (a `reachable_cheaper` scan
        plus several `Decimal` operations per bypassed candidate) can be
        skipped entirely for the many candidate transitions that lose this
        check, rather than built eagerly for every transition and then
        thrown away. Reason computation only ever happens for a
        transition already confirmed to win -- the reasons themselves are
        unaffected, only *when* they are computed."""
        existing = states[target_index].get(arrival_fuel_ticks)
        if existing is None:
            return True
        if new_key is existing.key:
            # The overwhelmingly common "buy nothing" pass-through: several
            # levels/targets from the same predecessor propagate the exact
            # same key tuple object onward. Object identity trivially
            # implies "not strictly less" -- skip the Decimal-comparing
            # `_key_less` call entirely rather than re-deriving the same
            # answer from scratch.
            return False
        return _key_less(new_key, existing.key)

    def relax(target_index, arrival_fuel_ticks, new_key, predecessor, edge):
        """Unconditionally re-checks and commits -- used only where the
        caller has not already called `_wins` itself (the "buy nothing"
        pass-through path and START's one-time setup, both of which pass
        `edge=None` and have nothing expensive to defer)."""
        if _wins(target_index, arrival_fuel_ticks, new_key):
            states[target_index][arrival_fuel_ticks] = _StateRecord(
                key=new_key, predecessor=predecessor, edge=edge
            )

    # START is non-purchasable: exactly one fixed departure level. Every
    # node within that fixed range -- not just the farthest -- gets its
    # own state (see reachable_targets' docstring above). One-time, not on
    # the hot path, so it stays in the original Decimal `reachable_targets`
    # form -- only the resulting arrival level is stored as ticks.
    start_ahead = nodes_ahead_of(-1)
    for target_pos, target_index in reachable_targets(start_ahead, Decimal(0), start_fuel):
        relax(
            target_index,
            start_fuel_ticks - full_ticks[target_index],
            start_key,
            (-1, start_fuel_ticks),
            None,
        )

    for node_index in range(node_count):
        station = ordered[node_index]
        pos = positions[node_index]
        pos_ticks = positions_ticks[node_index]
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

        for fuel_on_arrival_ticks, record in list(states[node_index].items()):
            fuel_on_arrival = _from_ticks(fuel_on_arrival_ticks)
            levels = useful_fill_levels_mi(
                pos,
                fuel_on_arrival,
                tank_range_mi=tank_range_mi,
                nodes_ahead_mi=[p for (p, idx) in ahead],
            )
            for level in levels:
                level_ticks = _to_ticks(level)
                target_range = _reachable_ticks(node_index, pos_ticks + level_ticks)
                if not target_range:
                    continue

                buy_mi = level - fuel_on_arrival
                is_purchase = buy_mi > 0
                predecessor = (node_index, fuel_on_arrival_ticks)

                if not is_purchase:
                    for target_index in target_range:
                        relax(
                            target_index,
                            level_ticks - (full_ticks[target_index] - pos_ticks),
                            record.key,
                            predecessor,
                            None,
                        )
                    continue

                gallons = buy_mi / mpg
                cost = gallons * station.price_per_gallon
                is_full_fill = level == tank_range_mi
                new_key = (
                    record.key[0] + cost + penalty,
                    record.key[1] + 1,
                    record.key[2] + (pos,),
                    record.key[3] + (station.opis_id,),
                )
                reachable_cheaper_idx = {idx for (_p, idx) in reachable_cheaper}

                # The reason and its bypassed-cheaper counters explain THIS
                # purchase decision, computed PER TARGET (not once per
                # level): a purchase that reaches multiple simultaneous
                # nodes (e.g. a full tank that both tops past an
                # intermediate candidate AND reaches FINISH, or that
                # coincidentally lands exactly on a reachable-cheaper
                # station) can have a genuinely different reason for each
                # target it reaches.
                for target_index in target_range:
                    arrival_fuel_ticks = level_ticks - (
                        full_ticks[target_index] - pos_ticks
                    )
                    # Inlined `_wins` (same three checks, same order, same
                    # meaning -- see its docstring) to shave one Python
                    # function-call layer off this loop's dominant share
                    # (~97%) of every purchase-transition attempt. Losing
                    # transitions are never on the winning path, so their
                    # purchase-reason story is never observed by any
                    # caller -- skip computing it. Values are unaffected
                    # either way, only whether this attempt's own reason
                    # is ever built.
                    existing = states[target_index].get(arrival_fuel_ticks)
                    if existing is not None:
                        if new_key is existing.key:
                            continue
                        if not _key_less(new_key, existing.key):
                            continue

                    target_pos = (
                        positions[target_index]
                        if target_index < node_count
                        else total_route_mi
                    )
                    bypassed_count = 0
                    bypassed_saving = None

                    if target_index == finish:
                        # REACH_FINISH always overrides -- this edge's
                        # purpose is completing the trip, not routing
                        # toward a named station.
                        reason = PurchaseReason.REACH_FINISH
                        reason_target_opis_id = None
                        reason_target_name = None
                    elif not is_full_fill:
                        reason = PurchaseReason.REACH_CHEAPER_STOP
                        reason_target_opis_id = ordered[target_index].opis_id
                        reason_target_name = ordered[target_index].name
                    elif target_index in reachable_cheaper_idx:
                        # The full-fill's exact-reach amount coincides
                        # with a reachable-cheaper station's own position
                        # (e.g. that station sits exactly one tank away).
                        # Landing there is REACHING that cheaper station,
                        # not bypassing it -- nothing was flown past.
                        reason = PurchaseReason.REACH_CHEAPER_STOP
                        reason_target_opis_id = ordered[target_index].opis_id
                        reason_target_name = ordered[target_index].name
                    else:
                        # Full fill, target is not itself a reachable
                        # cheaper station. Only stations strictly BEFORE
                        # this target were actually flown past by this
                        # specific edge -- a reachable-cheaper station at
                        # or beyond the target was never bypassed here.
                        truly_bypassed = [
                            (p, idx)
                            for (p, idx) in reachable_cheaper
                            if p < target_pos
                        ]
                        saving_total = Decimal(0)
                        for cheaper_pos, cheaper_idx in truly_bypassed:
                            cheaper_gap = cheaper_pos - pos
                            cheaper_buy_mi = max(
                                Decimal(0), cheaper_gap - fuel_on_arrival
                            )
                            cheaper_gallons = cheaper_buy_mi / mpg
                            saving_total += cheaper_gallons * (
                                station.price_per_gallon
                                - ordered[cheaper_idx].price_per_gallon
                            )
                        if truly_bypassed and penalty > saving_total:
                            # A genuine bypass AND the flat per-stop
                            # penalty strictly outweighs the fuel-dollar
                            # saving those bypassed stations offered --
                            # both conditions, never bypass-count alone.
                            # saving_total is never negative (each term is
                            # a nonnegative gallon count times a strictly
                            # positive price difference), so this branch
                            # can never fire at penalty=0.
                            reason = PurchaseReason.BYPASS_CHEAPER_NOT_WORTH_STOP
                            bypassed_count = len(truly_bypassed)
                            bypassed_saving = saving_total
                        elif ahead_has_cheaper:
                            reason = PurchaseReason.FILL_TO_CONTINUE
                        else:
                            reason = PurchaseReason.TOP_UP_AT_CHEAPEST
                        reason_target_opis_id = ordered[target_index].opis_id
                        reason_target_name = ordered[target_index].name

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
                    # Already confirmed a winner by `_wins` above, with
                    # nothing else touching this exact (target_index,
                    # arrival_fuel_ticks) state in between -- commit
                    # directly rather than re-checking through `relax`.
                    states[target_index][arrival_fuel_ticks] = _StateRecord(
                        key=new_key, predecessor=predecessor, edge=edge
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
