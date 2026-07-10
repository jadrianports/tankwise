"""Pure fuel-stop solver: cheapest feasible fueling plan (FUEL-01..07).

Request-path math only -- no Django, no DB, no HTTP client (FUEL-05). All
money and gallon values are exact, unrounded `Decimal`; rounding to cents
happens only at Phase 4's serialization boundary (D-08/D-09).
"""
from dataclasses import dataclass
from decimal import Decimal

from routing.services.exceptions import InfeasibleRouteError, InvalidRouteInputError


@dataclass(frozen=True)
class Candidate:
    """A candidate fuel stop positioned along the route."""

    name: str
    opis_id: int
    price_per_gallon: Decimal
    distance_from_start_mi: Decimal


@dataclass(frozen=True)
class FuelStop:
    """A purchase recorded at a real, along-route station."""

    name: str
    opis_id: int
    price_per_gallon: Decimal
    distance_from_start_mi: Decimal
    gallons: Decimal
    cost: Decimal


@dataclass(frozen=True)
class FuelPlan:
    """The cheapest feasible fueling plan for a route."""

    stops: list[FuelStop]
    total_cost: Decimal
    total_gallons: Decimal


def _as_decimal(value):
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _validate(candidates, total_route_mi, tank_range_mi, mpg):
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
) -> FuelPlan:
    """Return the cheapest feasible fueling plan for a route of
    ``total_route_mi`` miles, given an iterable of ``Candidate`` stations.

    Raises ``InvalidRouteInputError`` on malformed input and
    ``InfeasibleRouteError`` when no feasible plan exists (a gap between two
    along-route nodes exceeds ``tank_range_mi``).
    """
    total_route_mi = _as_decimal(total_route_mi)
    tank_range_mi = _as_decimal(tank_range_mi)
    mpg = _as_decimal(mpg)

    candidates = list(candidates)

    _validate(candidates, total_route_mi, tank_range_mi, mpg)

    ordered = sorted(
        candidates,
        key=lambda c: (c.distance_from_start_mi, c.price_per_gallon, c.opis_id),
    )

    pos = Decimal(0)
    fuel = tank_range_mi
    price_here = Decimal(0)
    current_name = "START"
    current_opis_id = None
    stops = []

    while True:
        reachable = [
            c for c in ordered if pos < c.distance_from_start_mi <= pos + tank_range_mi
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
                    )
                )
            fuel = fuel + buy_mi - gap
            pos = target.distance_from_start_mi
            price_here = target.price_per_gallon
            current_name = target.name
            current_opis_id = target.opis_id
            continue

        if (total_route_mi - pos) <= tank_range_mi:
            # (b) finish reachable, no cheaper station first -- buy just
            # enough to finish, never fill (endpoint rule, D-04.1).
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
                    )
                )
            break

        # (c) no cheaper station in range and finish out of range.
        if not reachable:
            remaining_nodes = [
                (c.distance_from_start_mi, c.name) for c in ordered if c.distance_from_start_mi > pos
            ]
            remaining_nodes.append((total_route_mi, "FINISH"))
            next_dist, next_name = min(remaining_nodes, key=lambda n: n[0])
            raise InfeasibleRouteError(
                from_station=current_name,
                to_station=next_name,
                gap_mi=next_dist - pos,
                max_range_mi=tank_range_mi,
            )

        # Fill the tank, then hop to the cheapest reachable station (ties
        # broken by nearest, D-05) -- never the farthest.
        buy_mi = tank_range_mi - fuel
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
                )
            )
        fuel = tank_range_mi
        target = min(
            reachable,
            key=lambda c: (c.price_per_gallon, c.distance_from_start_mi, c.opis_id),
        )
        fuel -= target.distance_from_start_mi - pos
        pos = target.distance_from_start_mi
        price_here = target.price_per_gallon
        current_name = target.name
        current_opis_id = target.opis_id

    return FuelPlan(
        stops=stops,
        total_cost=sum((s.cost for s in stops), Decimal(0)),
        total_gallons=sum((s.gallons for s in stops), Decimal(0)),
    )
