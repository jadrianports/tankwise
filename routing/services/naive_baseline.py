"""Pure price-blind fuel-stop baseline: the driver a savings figure is
measured against.

Request-path math only -- no Django, no DB, no HTTP client. All money and
gallon values are exact, unrounded `Decimal`; rounding to cents happens only
at the HTTP response serialization boundary. This module mirrors
`routing.services.solver`'s purity and Decimal discipline and differs only
in the fueling strategy, which never consults price.

The strategy: run the tank down, stop at the farthest reachable station,
fill completely, repeat -- never the nearest, never the cheapest. This is
the realistic behavior of a driver who ignores price entirely, and it is
the strategy that makes the savings delta (`compute_savings`) mean "the
value of price-awareness" and nothing else. A naive driver stopping at the
cheapest reachable station would already be price-aware, which would
corrupt the comparison.

The reachable set at any position is always bounded by the fuel actually on
board (`fuel`), never by the tank's full capacity (`tank_range_mi`) -- this
holds at START just as much as at a real station, since `fuel` already
starts at `starting_fuel * tank_range_mi` and is reset to `tank_range_mi`
only after a real fill. Building this fuel-bounded from the first line is
what keeps this brand-new module from inheriting the pre-07-01 START
landmine that `solver.py` had to be fixed to avoid.
"""
from dataclasses import dataclass
from decimal import Decimal

from routing.services.exceptions import InfeasibleRouteError, InvalidRouteInputError
from routing.services.solver import Candidate, FuelPlan, FuelStop


def _as_decimal(value):
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _validate(candidates, total_route_mi, tank_range_mi, mpg, starting_fuel):
    if total_route_mi <= 0:
        raise InvalidRouteInputError(
            f"total_route_mi must be positive, got {total_route_mi}"
        )
    if tank_range_mi <= 0:
        raise InvalidRouteInputError(
            f"tank_range_mi must be positive, got {tank_range_mi}"
        )
    if mpg <= 0:
        raise InvalidRouteInputError(f"mpg must be positive, got {mpg}")
    if starting_fuel < 0 or starting_fuel > 1:
        raise InvalidRouteInputError(
            f"starting_fuel must be within [0, 1], got {starting_fuel}"
        )
    for c in candidates:
        if c.price_per_gallon < 0:
            raise InvalidRouteInputError(
                f"price_per_gallon must not be negative, got {c.price_per_gallon} "
                f"for candidate {c.name!r}"
            )
        if c.distance_from_start_mi < 0 or c.distance_from_start_mi > total_route_mi:
            raise InvalidRouteInputError(
                f"distance_from_start_mi ({c.distance_from_start_mi}) for candidate "
                f"{c.name!r} must be within [0, total_route_mi={total_route_mi}]"
            )


def solve(
    candidates,
    total_route_mi,
    *,
    tank_range_mi=Decimal(500),
    mpg=Decimal(10),
    starting_fuel=Decimal(1),
) -> FuelPlan:
    """Return the price-blind baseline fueling plan for a route of
    ``total_route_mi`` miles, given an iterable of ``Candidate`` stations.

    Never inspects ``price_per_gallon`` when choosing where to stop -- only
    when computing what a stop cost. Every stop tops the tank to capacity;
    no stop ever buys a partial amount. Raises ``InvalidRouteInputError`` on
    malformed input and ``InfeasibleRouteError`` when the tank runs dry with
    no reachable station ahead.
    """
    total_route_mi = _as_decimal(total_route_mi)
    tank_range_mi = _as_decimal(tank_range_mi)
    mpg = _as_decimal(mpg)
    starting_fuel = _as_decimal(starting_fuel)

    candidates = list(candidates)

    _validate(candidates, total_route_mi, tank_range_mi, mpg, starting_fuel)

    ordered = sorted(candidates, key=lambda c: (c.distance_from_start_mi, c.opis_id))

    pos = Decimal(0)
    fuel = starting_fuel * tank_range_mi
    current_name = "START"
    stops = []

    while True:
        if (total_route_mi - pos) <= fuel:
            # The trip completes on what is already in the tank -- no
            # further purchase is made.
            break

        reachable = [
            c for c in ordered if pos < c.distance_from_start_mi <= pos + fuel
        ]

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
                max_range_mi=fuel,
            )

        # Farthest reachable candidate -- never the cheapest, never the
        # nearest. Ties broken by opis_id (ascending) for determinism.
        target = max(reachable, key=lambda c: (c.distance_from_start_mi, -c.opis_id))

        fuel -= target.distance_from_start_mi - pos
        pos = target.distance_from_start_mi

        buy_mi = tank_range_mi - fuel
        gallons = buy_mi / mpg
        stops.append(
            FuelStop(
                name=target.name,
                opis_id=target.opis_id,
                price_per_gallon=target.price_per_gallon,
                distance_from_start_mi=pos,
                gallons=gallons,
                cost=gallons * target.price_per_gallon,
            )
        )
        fuel = tank_range_mi
        current_name = target.name

    return FuelPlan(
        stops=stops,
        total_cost=sum((s.cost for s in stops), Decimal(0)),
        total_gallons=sum((s.gallons for s in stops), Decimal(0)),
    )


@dataclass(frozen=True)
class Savings:
    """The comparison between an optimized plan and the price-blind
    baseline, isolating fueling strategy as the sole variable."""

    amount: Decimal
    percent: Decimal | None
    naive_total_cost: Decimal
    naive_total_gallons: Decimal
    naive_stop_count: int


def compute_savings(optimal_plan, naive_plan) -> Savings:
    """Return the exact, unrounded savings of ``optimal_plan`` over
    ``naive_plan``.

    ``percent`` is ``None`` when the naive total cost is zero -- a $0 naive
    trip means the tank alone covered the route, a legitimate result rather
    than a division error.
    """
    naive_total_cost = naive_plan.total_cost
    amount = naive_total_cost - optimal_plan.total_cost
    percent = amount / naive_total_cost if naive_total_cost != 0 else None
    return Savings(
        amount=amount,
        percent=percent,
        naive_total_cost=naive_total_cost,
        naive_total_gallons=naive_plan.total_gallons,
        naive_stop_count=len(naive_plan.stops),
    )
