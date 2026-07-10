"""Pure fuel-stop solver: cheapest feasible fueling plan (FUEL-01..07).

Request-path math only -- no Django, no DB, no HTTP client (FUEL-05). All
money and gallon values are exact, unrounded `Decimal`; rounding to cents
happens only at Phase 4's serialization boundary (D-08/D-09).
"""
from dataclasses import dataclass
from decimal import Decimal


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
    ``InfeasibleRouteError`` when no feasible plan exists.
    """
    raise NotImplementedError("solve() algorithm is implemented in Task 2")
