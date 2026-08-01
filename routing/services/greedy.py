"""Production fallback fuel-stop solver: the pre-Phase-18 greedy,
restored as a reachable production code path for routes whose fixed-charge
DP would exceed the pre-flight latency budget
(`routing.services.dp.DP_TRANSITION_BUDGET`,
`routing.services.dp.estimate_transition_count`).

This is an independent restoration of the same algorithm
`routing/tests/frozen_greedy.py` freezes for differential testing --
it is never imported from that test-only module, and that module is never
imported here or anywhere else in production (see its own docstring).
Both modules implement the identical pre-Phase-18 greedy loop; this one
reuses `solver.py`'s own `Candidate`/`FuelStop`/`FuelPlan`/`PurchaseReason`
dataclasses (rather than redeclaring them, as the test-only frozen copy
does) so its output slots directly into `solver.solve()`'s existing
post-processing (the `corridor_avg_price`/`price_percentile`/
`skipped_count`/`skipped_avg_price` rebuild over the full candidate list)
unmodified -- exactly the same "raw plan" contract `dp.solve_fixed_charge`
already satisfies.

Request-path math only -- no Django, no DB, no HTTP client, exactly as
`solver.py`/`dp.py`/`prune.py`.
"""
from decimal import Decimal

from routing.services.exceptions import InfeasibleRouteError
from routing.services.solver import FuelPlan, FuelStop, PurchaseReason


def solve_greedy(
    candidates, total_route_mi, *, tank_range_mi, mpg, starting_fuel, penalty=Decimal(0)
) -> FuelPlan:
    """Return the cheapest-under-the-greedy-heuristic feasible fueling
    plan for a route of `total_route_mi` miles, given an iterable of
    `Candidate` stations. Structurally identical to the algorithm
    `solve()` ran before Phase 18 (and to `routing/tests/frozen_greedy.py`,
    its frozen differential referee): at every station, route to the
    nearest strictly-cheaper reachable station if one exists; otherwise
    fill to capacity and hop to the cheapest reachable station; buy just
    enough to finish once FINISH is reachable with no cheaper station
    first.

    This algorithm is structurally UNAWARE of any per-stop penalty -- it
    was never fixed-charge-aware, before or after Phase 18 -- so it is not
    guaranteed optimal under the fixed-charge objective `solve_fixed_charge`
    minimises. It exists as a bounded, near-linear-time fallback for
    inputs where an exact fixed-charge solve is not practical inside the
    request's latency budget (see `routing.services.dp`'s
    "Pre-flight transition-count estimate" section), trading exactness for
    a plan that is still feasible, still cheap by construction (a proven
    pure-fuel-cost optimum at `penalty=0`, per `test_solver_optimality.py`),
    and still fully explained by the same `purchase_reason` vocabulary the
    DP uses -- it simply never emits `BYPASS_CHEAPER_NOT_WORTH_STOP`,
    since it never reasons about a stop's fixed cost at all.

    `penalty` is accepted ONLY to compute the reported
    `penalised_objective`/`penalty_applied` fields on the returned
    `FuelPlan` (`total_cost + penalty * len(stops)`, the identical formula
    `solve_fixed_charge` uses) so a caller comparing a greedy-fallback
    plan's reported objective to a DP plan's reads the same formula either
    way -- it never influences which stations this algorithm buys at or
    how much.

    Assumes the caller has already run `dp.preflight_gap_check` (or
    equivalent) over `candidates`/`total_route_mi`/`tank_range_mi`/
    `starting_fuel` -- `solver.solve()`, this function's only production
    caller, always does. This function still raises `InfeasibleRouteError`
    itself if a genuine gap is encountered regardless (defensive, matching
    the frozen greedy's own behaviour), rather than assuming that
    precondition silently.

    `candidates`/`total_route_mi`/`tank_range_mi`/`mpg`/`starting_fuel`/
    `penalty` are assumed already-validated, already-`Decimal` values --
    exactly the same assumption `dp.solve_fixed_charge` makes about its
    own inputs. Validation is `solver.solve()`'s sole responsibility.

    Only `name`, `opis_id`, `price_per_gallon`, `distance_from_start_mi`,
    `gallons`, `cost`, `purchase_reason`, `reason_target_opis_id`, and
    `reason_target_name` are populated on each returned `FuelStop` --
    `skipped_count`, `skipped_avg_price`, `price_percentile`, and
    `corridor_avg_price` are left at their dataclass defaults, exactly as
    `dp.solve_fixed_charge` leaves them, because `solver.solve()` rebuilds
    those over the FULL, unpruned candidate list itself (D-20) regardless
    of which producer built the raw plan.
    """
    candidates = list(candidates)

    ordered = sorted(
        candidates,
        key=lambda c: (c.distance_from_start_mi, c.price_per_gallon, c.opis_id),
    )

    pos = Decimal(0)
    fuel = starting_fuel * tank_range_mi
    price_here = Decimal(0)
    current_name = "START"
    current_opis_id = None
    stops = []

    while True:
        usable_range = tank_range_mi if current_opis_id is not None else fuel
        reachable = [
            c for c in ordered if pos < c.distance_from_start_mi <= pos + usable_range
        ]
        cheaper = [c for c in reachable if c.price_per_gallon < price_here]

        if cheaper:
            # (a) nearest strictly-cheaper reachable station.
            target = min(
                cheaper,
                key=lambda c: (c.distance_from_start_mi, c.price_per_gallon, c.opis_id),
            )
            gap = target.distance_from_start_mi - pos
            buy_mi = max(Decimal(0), gap - fuel)
            if buy_mi > 0:
                gallons = buy_mi / mpg
                stops.append(
                    FuelStop(
                        name=current_name,
                        opis_id=current_opis_id,
                        price_per_gallon=price_here,
                        distance_from_start_mi=pos,
                        gallons=gallons,
                        cost=gallons * price_here,
                        purchase_reason=PurchaseReason.REACH_CHEAPER_STOP,
                        reason_target_opis_id=target.opis_id,
                        reason_target_name=target.name,
                    )
                )
            fuel = fuel + buy_mi - gap
            pos = target.distance_from_start_mi
            price_here = target.price_per_gallon
            current_name = target.name
            current_opis_id = target.opis_id
            continue

        if (total_route_mi - pos) <= usable_range:
            # (b) finish reachable, no cheaper station first -- buy just
            # enough to finish, never fill (endpoint rule).
            gap = total_route_mi - pos
            buy_mi = max(Decimal(0), gap - fuel)
            if buy_mi > 0:
                gallons = buy_mi / mpg
                stops.append(
                    FuelStop(
                        name=current_name,
                        opis_id=current_opis_id,
                        price_per_gallon=price_here,
                        distance_from_start_mi=pos,
                        gallons=gallons,
                        cost=gallons * price_here,
                        purchase_reason=PurchaseReason.REACH_FINISH,
                        reason_target_opis_id=None,
                        reason_target_name=None,
                    )
                )
            break

        # (c) no cheaper station in range and finish out of range.
        if not reachable:
            remaining_nodes = [
                (c.distance_from_start_mi, c.name)
                for c in ordered
                if c.distance_from_start_mi > pos
            ]
            remaining_nodes.append((total_route_mi, "FINISH"))
            next_dist, next_name = min(remaining_nodes, key=lambda n: n[0])
            raise InfeasibleRouteError(
                from_station=current_name,
                to_station=next_name,
                gap_mi=next_dist - pos,
                max_range_mi=usable_range,
            )

        # Fill the tank (only possible at a real, purchasable station --
        # START can never be billed), then hop to the cheapest reachable
        # station (ties broken by nearest) -- never the farthest.
        target = min(
            reachable,
            key=lambda c: (c.price_per_gallon, c.distance_from_start_mi, c.opis_id),
        )
        if current_opis_id is not None:
            buy_mi = tank_range_mi - fuel
            if buy_mi > 0:
                gallons = buy_mi / mpg
                ahead = [c for c in candidates if c.distance_from_start_mi > pos]
                if not ahead or price_here <= min(c.price_per_gallon for c in ahead):
                    reason = PurchaseReason.TOP_UP_AT_CHEAPEST
                else:
                    reason = PurchaseReason.FILL_TO_CONTINUE
                stops.append(
                    FuelStop(
                        name=current_name,
                        opis_id=current_opis_id,
                        price_per_gallon=price_here,
                        distance_from_start_mi=pos,
                        gallons=gallons,
                        cost=gallons * price_here,
                        purchase_reason=reason,
                        reason_target_opis_id=target.opis_id,
                        reason_target_name=target.name,
                    )
                )
            fuel = tank_range_mi
        fuel -= target.distance_from_start_mi - pos
        pos = target.distance_from_start_mi
        price_here = target.price_per_gallon
        current_name = target.name
        current_opis_id = target.opis_id

    total_cost = sum((s.cost for s in stops), Decimal(0))
    total_gallons = sum((s.gallons for s in stops), Decimal(0))

    return FuelPlan(
        stops=stops,
        total_cost=total_cost,
        total_gallons=total_gallons,
        penalised_objective=total_cost + penalty * len(stops),
        penalty_applied=penalty,
    )
